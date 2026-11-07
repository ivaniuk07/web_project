"""Job lifecycle, independent of HTTP views and templates."""
import logging
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .models import Job
from .processing import ProcessingError, burn_subtitles, generate_subtitles

logger = logging.getLogger(__name__)


def create_job(owner, video):
    job = Job(owner=owner, original_name=Path(video.name).name)
    job.source.save(video.name, video, save=False)
    try:
        job.save()
    except Exception:
        job.source.delete(save=False)
        raise
    return job


def process_job(job):
    # This atomic transition prevents double submission without holding a lock
    # throughout expensive FFmpeg/Whisper processing.
    claimed = Job.objects.filter(pk=job.pk, status__in=[Job.Status.QUEUED, Job.Status.FAILED]).update(
        status=Job.Status.RUNNING, error='', updated_at=timezone.now(),
    )
    if not claimed:
        return
    job.refresh_from_db()
    captions_name = job.subtitles.name or f'subtitles/{job.pk}.srt'
    result_name = f'rendered/{job.pk}.mp4'
    captions = Path(settings.MEDIA_ROOT) / captions_name
    output = Path(settings.MEDIA_ROOT) / result_name
    try:
        if not captions.is_file():
            generate_subtitles(Path(job.source.path), captions)
        # Retain a valid SRT even if video rendering subsequently fails.
        Job.objects.filter(pk=job.pk).update(subtitles=captions_name, updated_at=timezone.now())
        burn_subtitles(Path(job.source.path), captions, output)
    except Exception as exc:
        logger.exception('Job %s failed', job.pk)
        error = str(exc) if isinstance(exc, ProcessingError) else 'Помилка обробки. Спробуйте ще раз.'
        Job.objects.filter(pk=job.pk).update(status=Job.Status.FAILED, error=error, updated_at=timezone.now())
    else:
        Job.objects.filter(pk=job.pk).update(status=Job.Status.SUCCEEDED, result=result_name, error='', updated_at=timezone.now())
