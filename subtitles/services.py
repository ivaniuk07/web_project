"""Job lifecycle, independent of HTTP views and templates."""
import logging
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Job
from .processing import ProcessingBudget, ProcessingError, burn_subtitles, generate_subtitles
from .validation import inspect_video, validate_upload

logger = logging.getLogger(__name__)


def create_job(owner, video):
    validate_upload(video)
    job = Job(owner=owner, original_name=Path(video.name).name)
    job.source.save(video.name, video, save=False)
    try:
        duration = inspect_video(Path(job.source.path))
        if duration > settings.MAX_VIDEO_SECONDS:
            raise ValidationError(f'Максимальна тривалість відео — {settings.MAX_VIDEO_SECONDS} с.')
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=owner.pk)
            _check_capacity(owner)
            job.save()
    except Exception:
        job.source.delete(save=False)
        raise
    return job


def _check_capacity(owner, exclude_job=None):
    active = Job.objects.filter(owner=owner, status__in=[Job.Status.QUEUED, Job.Status.RUNNING])
    if exclude_job is not None:
        active = active.exclude(pk=exclude_job)
    if active.count() >= settings.MAX_ACTIVE_JOBS_PER_USER:
        raise ValidationError(f'Досягнуто ліміт активних завдань — {settings.MAX_ACTIVE_JOBS_PER_USER}. Дочекайтеся завершення поточних задач.')


def process_job(job):
    # This atomic transition prevents double submission without holding a lock
    # throughout expensive FFmpeg/Whisper processing.
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=job.owner_id)
        job.refresh_from_db()
        if job.status not in [Job.Status.QUEUED, Job.Status.FAILED]:
            return
        if job.status == Job.Status.FAILED:
            _check_capacity(job.owner, exclude_job=job.pk)
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
        budget = ProcessingBudget()
        budget.remaining(settings.JOB_TIMEOUT_SECONDS)
        # Revalidate older/imported jobs and changed limits before execution.
        validate_upload(job.source)
        duration = inspect_video(Path(job.source.path), timeout=budget.remaining(settings.MEDIA_PROBE_TIMEOUT))
        if duration > settings.MAX_VIDEO_SECONDS:
            raise ProcessingError(f'Максимальна тривалість відео — {settings.MAX_VIDEO_SECONDS} с.')
        if not captions.is_file():
            generate_subtitles(Path(job.source.path), captions, budget=budget)
        # Retain a valid SRT even if video rendering subsequently fails.
        Job.objects.filter(pk=job.pk).update(subtitles=captions_name, updated_at=timezone.now())
        burn_subtitles(Path(job.source.path), captions, output, budget=budget)
        budget.remaining(settings.JOB_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.exception('Job %s failed', job.pk)
        if isinstance(exc, ValidationError):
            error = ' '.join(exc.messages)
        else:
            error = str(exc) if isinstance(exc, ProcessingError) else 'Помилка обробки. Спробуйте ще раз.'
        Job.objects.filter(pk=job.pk).update(status=Job.Status.FAILED, error=error, updated_at=timezone.now())
    else:
        Job.objects.filter(pk=job.pk).update(status=Job.Status.SUCCEEDED, result=result_name, error='', updated_at=timezone.now())
