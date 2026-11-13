"""Convert one video to SRT. Transport and web requests stay outside this module."""
import json
import math
import os
import shutil
import subprocess
import tempfile
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from django.conf import settings

from .subtitle_styles import SubtitleStyle


ASS_STYLE_OVERRIDES = {
    SubtitleStyle.YELLOW: 'PrimaryColour=&H004CDCFF,Bold=-1,Outline=2,Shadow=0',
    SubtitleStyle.DARK_BOX: 'BorderStyle=3,OutlineColour=&H30000000,BackColour=&H30000000,Outline=3,Shadow=0',
    SubtitleStyle.CINEMA: 'FontName=serif,FontSize=24,PrimaryColour=&H00D6F1FF,Italic=-1,Outline=1,Shadow=0',
    SubtitleStyle.NEON: 'FontSize=21,PrimaryColour=&H00F9E67C,OutlineColour=&H0037211C,Bold=-1,Outline=2,Shadow=0',
    SubtitleStyle.STUDIO: (
        'FontSize=19,PrimaryColour=&H00351717,Bold=-1,BorderStyle=3,'
        'OutlineColour=&H00FFCFC5,BackColour=&H00FFCFC5,Outline=3,Shadow=0'
    ),
}


class ProcessingError(Exception):
    """A message suitable for display on the upload page."""


class ProcessingTimeout(ProcessingError):
    pass


class ProcessingCancelled(ProcessingError):
    pass


class ProcessingBudget:
    def __init__(self, check_cancel=None, on_stage=None):
        self.deadline = time.monotonic() + settings.JOB_TIMEOUT_SECONDS
        self.check_cancel = check_cancel
        self.on_stage = on_stage

    def remaining(self, stage_limit):
        if self.check_cancel:
            self.check_cancel()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessingTimeout('Перевищено максимальний час обробки завдання.')
        return min(stage_limit, remaining)

    def set_stage(self, stage):
        self.remaining(settings.JOB_TIMEOUT_SECONDS)
        if self.on_stage:
            self.on_stage(stage)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


class SpeechRecognizer(Protocol):
    def recognize(self, audio: Path) -> list[Segment]: ...


class WhisperRecognizer:
    def __init__(self, budget, language=None):
        self.budget = budget
        self.language = language

    def recognize(self, audio):
        self.budget.set_stage('Розпізнавання мовлення')
        command = [
            sys.executable, '-m', 'subtitles.speech', str(audio.resolve()), settings.WHISPER_MODEL,
        ]
        if self.language:
            command.extend(['--language', self.language])
        result = _run(command, self.budget.remaining(settings.TRANSCRIBE_TIMEOUT), check_cancel=self.budget.check_cancel, cwd=settings.BASE_DIR)
        return [Segment(**item) for item in json.loads(result.stdout)]


def _run(command, timeout, check_cancel=None, **kwargs):
    try:
        if check_cancel is not None:
            return _run_cancellable(command, timeout, check_cancel, **kwargs)
        return subprocess.run(command, capture_output=True, text=True, check=True, timeout=timeout, **kwargs)
    except FileNotFoundError as exc:
        raise ProcessingError('Для обробки встановіть FFmpeg і FFprobe.') from exc
    except subprocess.TimeoutExpired as exc:
        raise ProcessingTimeout('Перевищено максимальний час обробки. Спробуйте коротше відео.') from exc
    except subprocess.CalledProcessError as exc:
        if 'No such filter' in (exc.stderr or ''):
            raise ProcessingError('Для вбудовування субтитрів потрібен FFmpeg із підтримкою libass.') from exc
        raise ProcessingError('Не вдалося обробити відео. Файл пошкоджений або формат не підтримується.') from exc
    except OSError as exc:
        raise ProcessingError('Не вдалося прочитати відео. Файл пошкоджений або формат не підтримується.') from exc


def _run_cancellable(command, timeout, check_cancel, **kwargs):
    check_cancel()
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
    try:
        while True:
            check_cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        check_cancel()
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def extract_audio(source: Path, destination: Path, budget=None):
    budget = budget or ProcessingBudget()
    budget.set_stage('Підготовка аудіо')
    metadata = _run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', str(source)], budget.remaining(settings.MEDIA_PROBE_TIMEOUT), check_cancel=budget.check_cancel)
    try:
        streams = json.loads(metadata.stdout)['streams']
    except (ValueError, KeyError, TypeError) as exc:
        raise ProcessingError('Не вдалося прочитати структуру відео.') from exc
    if not any(stream.get('codec_type') == 'audio' for stream in streams):
        raise ProcessingError('У відео немає аудіодоріжки.')
    _run([settings.FFMPEG_BINARY, '-nostdin', '-v', 'error', '-y', '-i', str(source), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(destination)], budget.remaining(settings.FFMPEG_TIMEOUT), check_cancel=budget.check_cancel)


def _timestamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}'


def render_srt(segments: list[Segment]) -> str:
    blocks = []
    for segment in segments:
        if not math.isfinite(segment.start) or not math.isfinite(segment.end) or segment.start < 0 or segment.end < segment.start:
            raise ProcessingError('Розпізнавання повернуло некоректні часові позначки.')
        text = '\n'.join(line.strip() for line in segment.text.splitlines() if line.strip())
        if text:
            blocks.append(f'{len(blocks) + 1}\n{_timestamp(segment.start)} --> {_timestamp(segment.end)}\n{text}\n\n')
    if not blocks:
        raise ProcessingError('Не вдалося розпізнати мовлення у відео.')
    return ''.join(blocks)


def generate_subtitles(source: Path, destination: Path, recognizer: SpeechRecognizer | None = None, budget=None, *, language=None):
    """A recognizer can be replaced without changing the conversion workflow."""
    budget = budget or ProcessingBudget()
    recognizer = recognizer or WhisperRecognizer(budget, language)
    try:
        with tempfile.TemporaryDirectory(prefix='subtitle-audio-') as directory:
            audio = Path(directory) / 'audio.wav'
            extract_audio(source, audio, budget=budget)
            segments = recognizer.recognize(audio)
            budget.set_stage('Формування SRT')
            content = render_srt(segments)
            budget.remaining(settings.JOB_TIMEOUT_SECONDS)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Publish only a complete file, even if multiple requests process one video.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent, delete=False) as output:
            temporary_path = Path(output.name)
            try:
                output.write(content)
                output.close()
                os.replace(temporary_path, destination)
            finally:
                temporary_path.unlink(missing_ok=True)
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError('Помилка розпізнавання. Перевірте доступ до моделі Whisper та спробуйте ще раз.') from exc


def burn_subtitles(source: Path, captions: Path, destination: Path, budget=None, *, subtitle_style=SubtitleStyle.CLASSIC):
    """Render captions into an H.264 MP4 and publish only a complete result."""
    budget = budget or ProcessingBudget()
    budget.set_stage('Вбудовування субтитрів')
    subtitle_filter = 'subtitles=filename=captions.srt'
    if overrides := ASS_STYLE_OVERRIDES.get(subtitle_style):
        subtitle_filter += f":force_style='{overrides}'"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='render-', dir=destination.parent) as directory:
        workdir = Path(directory).resolve()
        # A fixed relative subtitle name avoids FFmpeg filter escaping for user paths.
        shutil.copyfile(captions, workdir / 'captions.srt')
        output = workdir / 'result.mp4'
        _run([
            settings.FFMPEG_BINARY, '-nostdin', '-v', 'error', '-y',
            '-i', str(source.resolve()), '-map', '0:v:0', '-map', '0:a:0?',
            '-vf', f'{subtitle_filter},pad=ceil(iw/2)*2:ceil(ih/2)*2',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-movflags', '+faststart', str(output),
        ], budget.remaining(settings.VIDEO_RENDER_TIMEOUT), check_cancel=budget.check_cancel, cwd=workdir)
        budget.remaining(settings.JOB_TIMEOUT_SECONDS)
        os.replace(output, destination)
