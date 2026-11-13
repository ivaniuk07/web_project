"""Admission checks shared by forms and job creation."""
import json
import math

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator

from .processing import ProcessingCancelled, ProcessingError, _run


def validate_upload(video):
    FileExtensionValidator(settings.ALLOWED_VIDEO_EXTENSIONS)(video)
    if video.size <= 0:
        raise ValidationError('Файл порожній.')
    if video.size > settings.MAX_UPLOAD_BYTES:
        maximum = settings.MAX_UPLOAD_BYTES / (1024 * 1024)
        raise ValidationError(f'Максимальний розмір файлу — {maximum:g} МіБ.')


def inspect_video(path, timeout=None, check_cancel=None):
    try:
        metadata = _run([
            'ffprobe', '-v', 'error', '-show_entries',
            'stream=codec_type:format=duration', '-of', 'json', str(path),
        ], settings.MEDIA_PROBE_TIMEOUT if timeout is None else timeout, check_cancel=check_cancel)
        data = json.loads(metadata.stdout)
        kinds = {stream['codec_type'] for stream in data['streams']}
        duration = float(data['format']['duration'])
        if not {'video', 'audio'}.issubset(kinds) or not math.isfinite(duration) or duration <= 0:
            raise ValueError
        return duration
    except ProcessingCancelled:
        raise
    except ProcessingError as exc:
        raise ValidationError(str(exc)) from exc
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValidationError('Некоректне відео: потрібні відеодоріжка, аудіодоріжка та визначена тривалість.') from exc
