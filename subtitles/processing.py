"""Convert one video to SRT. Transport and web requests stay outside this module."""
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from django.conf import settings


class ProcessingError(Exception):
    """A message suitable for display on the upload page."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


class SpeechRecognizer(Protocol):
    def recognize(self, audio: Path) -> list[Segment]: ...


class WhisperRecognizer:
    def recognize(self, audio):
        from faster_whisper import WhisperModel
        model = WhisperModel(settings.WHISPER_MODEL, device='cpu', compute_type='int8')
        segments, _ = model.transcribe(str(audio), beam_size=1)
        return [Segment(item.start, item.end, item.text.strip()) for item in segments]


def _run(command, timeout, **kwargs):
    try:
        return subprocess.run(command, capture_output=True, text=True, check=True, timeout=timeout, **kwargs)
    except FileNotFoundError as exc:
        raise ProcessingError('Для обробки встановіть FFmpeg і FFprobe.') from exc
    except subprocess.TimeoutExpired as exc:
        raise ProcessingError('Перевищено час обробки. Спробуйте коротше відео.') from exc
    except subprocess.CalledProcessError as exc:
        if 'No such filter' in (exc.stderr or ''):
            raise ProcessingError('Для вбудовування субтитрів потрібен FFmpeg із підтримкою libass.') from exc
        raise ProcessingError('Не вдалося обробити відео. Файл пошкоджений або формат не підтримується.') from exc
    except OSError as exc:
        raise ProcessingError('Не вдалося прочитати відео. Файл пошкоджений або формат не підтримується.') from exc


def extract_audio(source: Path, destination: Path):
    metadata = _run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', str(source)], settings.MEDIA_PROBE_TIMEOUT)
    try:
        streams = json.loads(metadata.stdout)['streams']
    except (ValueError, KeyError, TypeError) as exc:
        raise ProcessingError('Не вдалося прочитати структуру відео.') from exc
    if not any(stream.get('codec_type') == 'audio' for stream in streams):
        raise ProcessingError('У відео немає аудіодоріжки.')
    _run([settings.FFMPEG_BINARY, '-nostdin', '-v', 'error', '-y', '-i', str(source), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(destination)], settings.FFMPEG_TIMEOUT)


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


def generate_subtitles(source: Path, destination: Path, recognizer: SpeechRecognizer | None = None):
    """A recognizer can be replaced without changing the conversion workflow."""
    recognizer = recognizer or WhisperRecognizer()
    try:
        with tempfile.TemporaryDirectory(prefix='subtitle-audio-') as directory:
            audio = Path(directory) / 'audio.wav'
            extract_audio(source, audio)
            content = render_srt(recognizer.recognize(audio))
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


def burn_subtitles(source: Path, captions: Path, destination: Path):
    """Render captions into an H.264 MP4 and publish only a complete result."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='render-', dir=destination.parent) as directory:
        workdir = Path(directory).resolve()
        # A fixed relative subtitle name avoids FFmpeg filter escaping for user paths.
        shutil.copyfile(captions, workdir / 'captions.srt')
        output = workdir / 'result.mp4'
        _run([
            settings.FFMPEG_BINARY, '-nostdin', '-v', 'error', '-y',
            '-i', str(source.resolve()), '-map', '0:v:0', '-map', '0:a:0?',
            '-vf', 'subtitles=filename=captions.srt,pad=ceil(iw/2)*2:ceil(ih/2)*2',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '23', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-movflags', '+faststart', str(output),
        ], settings.VIDEO_RENDER_TIMEOUT, cwd=workdir)
        os.replace(output, destination)
