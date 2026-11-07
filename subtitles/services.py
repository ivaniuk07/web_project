"""Job lifecycle, independent of HTTP views and templates."""
import logging
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Job
from .processing import ProcessingBudget, ProcessingCancelled, ProcessingError, burn_subtitles, generate_subtitles
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



def enqueue_job(job):
    from .tasks import process_video_task
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=job.owner_id)
        job = Job.objects.select_for_update().get(pk=job.pk)
        if job.status not in [Job.Status.QUEUED, Job.Status.FAILED]:
            return
        if job.status == Job.Status.QUEUED and job.task_token:
            return
        if job.status == Job.Status.FAILED:
            _check_capacity(job.owner, exclude_job=job.pk)
        job.status = Job.Status.QUEUED
        job.task_token = uuid.uuid4()
        job.stage = 'У черзі'
        job.error = ''
        job.save(update_fields=['status', 'task_token', 'stage', 'error', 'updated_at'])
    try:
        process_video_task.apply_async(
            args=[str(job.pk), job.source.name, str(job.task_token)],
            task_id=str(job.task_token), queue=settings.CELERY_TASK_DEFAULT_QUEUE,
            retry=False,
        )
    except Exception as exc:
        logger.exception('Could not enqueue job %s', job.pk)
        Job.objects.filter(pk=job.pk, task_token=job.task_token, status=Job.Status.QUEUED).update(
            status=Job.Status.FAILED, stage='Помилка черги',
            error='Черга недоступна. Спробуйте запустити завдання ще раз.', updated_at=timezone.now(),
        )
        raise ValidationError('Не вдалося передати задачу до черги. Спробуйте ще раз.') from exc


def cancel_job(job):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=job.owner_id)
        Job.objects.filter(pk=job.pk, status__in=[Job.Status.QUEUED, Job.Status.RUNNING]).update(
            status=Job.Status.CANCELLED, stage='Скасовано користувачем',
            task_token=None, error='', updated_at=timezone.now(),
        )


def process_job(job, token=None, node_name=''):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=job.owner_id)
        job.refresh_from_db()
        if job.status not in [Job.Status.QUEUED, Job.Status.FAILED]:
            return
        if token is not None and (job.status != Job.Status.QUEUED or str(job.task_token) != str(token)):
            return
        if token is None and job.task_token is not None:
            return
        if job.status == Job.Status.FAILED:
            _check_capacity(job.owner, exclude_job=job.pk)
        job.task_token = job.task_token or uuid.uuid4()
        job.status = Job.Status.RUNNING
        job.stage = 'Перевірка відео'
        job.node_name = node_name
        job.error = ''
        job.save(update_fields=['task_token', 'status', 'stage', 'node_name', 'error', 'updated_at'])
    token = job.task_token
    active = Job.objects.filter(pk=job.pk, task_token=token, status=Job.Status.RUNNING)

    def check_cancel():
        if not active.exists():
            raise ProcessingCancelled('Обробку скасовано.')

    def set_stage(stage):
        if not active.update(stage=stage, updated_at=timezone.now()):
            raise ProcessingCancelled('Обробку скасовано.')

    captions_name = job.subtitles.name or f'subtitles/{job.pk}-{token}.srt'
    result_name = f'rendered/{job.pk}-{token}.mp4'
    captions = Path(settings.MEDIA_ROOT) / captions_name
    output = Path(settings.MEDIA_ROOT) / result_name
    completed = False
    cancelled = False
    try:
        budget = ProcessingBudget(check_cancel=check_cancel, on_stage=set_stage)
        budget.remaining(settings.JOB_TIMEOUT_SECONDS)
        validate_upload(job.source)
        duration = inspect_video(Path(job.source.path), timeout=budget.remaining(settings.MEDIA_PROBE_TIMEOUT), check_cancel=check_cancel)
        if duration > settings.MAX_VIDEO_SECONDS:
            raise ProcessingError(f'Максимальна тривалість відео — {settings.MAX_VIDEO_SECONDS} с.')
        if not captions.is_file():
            generate_subtitles(Path(job.source.path), captions, budget=budget)
        check_cancel()
        if not active.update(subtitles=captions_name, updated_at=timezone.now()):
            raise ProcessingCancelled('Обробку скасовано.')
        burn_subtitles(Path(job.source.path), captions, output, budget=budget)
        budget.remaining(settings.JOB_TIMEOUT_SECONDS)
        if not active.update(status=Job.Status.SUCCEEDED, stage='Готово', result=result_name, error='', updated_at=timezone.now()):
            raise ProcessingCancelled('Обробку скасовано.')
        completed = True
    except ProcessingCancelled:
        cancelled = True
    except Exception as exc:
        logger.exception('Job %s failed', job.pk)
        if isinstance(exc, ValidationError):
            error = ' '.join(exc.messages)
        else:
            error = str(exc) if isinstance(exc, ProcessingError) else 'Помилка обробки. Спробуйте ще раз.'
        active.update(status=Job.Status.FAILED, stage='Помилка обробки', error=error, updated_at=timezone.now())
    finally:
        if not completed:
            output.unlink(missing_ok=True)
        if cancelled and not job.subtitles:
            captions.unlink(missing_ok=True)
