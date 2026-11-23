from pathlib import Path
import logging
import uuid

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .dispatcher import dispatch_next_job
from .models import Job, SubtitleLanguage, SubtitleStyle
from .processing import (
    ProcessingBudget,
    ProcessingCancelled,
    ProcessingError,
    burn_subtitles,
    generate_subtitles,
    get_video_duration,
    validate_video_upload,
)


logger = logging.getLogger(__name__)
WAITING_FOR_NODE = 'Очікування доступного вузла'


def lock_owner(owner_id):
    get_user_model().objects.select_for_update().get(pk=owner_id)


def create_job(owner, video, *, subtitle_style=SubtitleStyle.CLASSIC, subtitle_language=SubtitleLanguage.AUTO):
    validate_video_upload(video)
    job = Job(
        owner=owner, original_name=Path(video.name).name,
        subtitle_style=subtitle_style, subtitle_language=subtitle_language,
    )
    job.source.save(video.name, video, save=False)
    try:
        duration = get_video_duration(Path(job.source.path))
        if duration > settings.MAX_VIDEO_SECONDS:
            raise ValidationError(f'Максимальна тривалість відео — {settings.MAX_VIDEO_SECONDS} с.')
        job.duration_seconds = duration
        with transaction.atomic():
            lock_owner(owner.pk)
            check_user_job_limit(owner)
            job.save()
    except Exception:
        job.source.delete(save=False)
        raise
    return job


def check_user_job_limit(owner, exclude_job=None):
    active_jobs = Job.objects.filter(owner=owner, status__in=Job.ACTIVE)
    if exclude_job is not None:
        active_jobs = active_jobs.exclude(pk=exclude_job)
    if active_jobs.count() >= settings.MAX_ACTIVE_JOBS_PER_USER:
        raise ValidationError(f'Досягнуто ліміт активних завдань — {settings.MAX_ACTIVE_JOBS_PER_USER}. Дочекайтеся завершення поточних задач.')


def queue_job(job):
    with transaction.atomic():
        lock_owner(job.owner_id)
        job = Job.objects.select_for_update().get(pk=job.pk)
        if job.status not in Job.CAN_START:
            return
        if job.status == Job.Status.FAILED:
            check_user_job_limit(job.owner, exclude_job=job.pk)
        job.status = Job.Status.QUEUED
        job.task_token = uuid.uuid4()
        job.stage = WAITING_FOR_NODE
        job.error = ''
        job.node = None
        job.node_name = ''
        job.dispatch_attempts = 0
        job.dispatched_at = None
        job.published_at = None
        job.started_at = None
        job.finished_at = None
        job.save()
    # Persisted intent survives web/process failures; dispatcher retries it.
    dispatch_next_job(job_id=job.pk)


def cancel_job(job):
    with transaction.atomic():
        lock_owner(job.owner_id)
        Job.objects.filter(pk=job.pk, status__in=Job.ACTIVE).update(
            status=Job.Status.CANCELLED, stage='Скасовано користувачем',
            task_token=None, error='', updated_at=timezone.now(), finished_at=timezone.now(),
        )


def start_job(job, token, node_name):
    with transaction.atomic():
        lock_owner(job.owner_id)
        job = Job.objects.select_for_update().get(pk=job.pk)
        if job.status not in [Job.Status.UPLOADED, Job.Status.QUEUED, Job.Status.FAILED]:
            return
        if token is not None and (job.status != Job.Status.QUEUED or str(job.task_token) != str(token)):
            return
        if token is None and job.task_token is not None:
            return
        if job.status == Job.Status.FAILED:
            check_user_job_limit(job.owner, exclude_job=job.pk)
        job.task_token = job.task_token or uuid.uuid4()
        job.status = Job.Status.RUNNING
        job.stage = 'Перевірка відео'
        job.node_name = job.node.name if job.node_id else node_name
        job.error = ''
        job.started_at = timezone.now()
        job.finished_at = None
        job.save(update_fields=['task_token', 'status', 'stage', 'node_name', 'error', 'started_at', 'finished_at', 'updated_at'])
        return job


def process_job(job, token=None, node_name=''):
    job = start_job(job, token, node_name)
    if job is None:
        return
    current_attempt = Job.objects.filter(pk=job.pk, task_token=job.task_token, status=Job.Status.RUNNING)

    def check_job_cancelled():
        if not current_attempt.exists():
            raise ProcessingCancelled('Обробку скасовано.')

    def save_progress(**fields):
        if not current_attempt.update(**fields, updated_at=timezone.now()):
            raise ProcessingCancelled('Обробку скасовано.')

    def set_stage(stage):
        save_progress(stage=stage)

    captions_name = job.subtitles.name or f'subtitles/{job.pk}-{job.task_token}.srt'
    result_name = f'rendered/{job.pk}-{job.task_token}.mp4'
    captions = Path(settings.MEDIA_ROOT) / captions_name
    output = Path(settings.MEDIA_ROOT) / result_name
    completed = False
    cancelled = False
    try:
        budget = ProcessingBudget(check_cancel=check_job_cancelled, on_stage=set_stage)
        budget.remaining_seconds(settings.JOB_TIMEOUT_SECONDS)
        validate_video_upload(job.source)
        source = Path(job.source.path)
        duration = get_video_duration(source, timeout=budget.remaining_seconds(settings.MEDIA_PROBE_TIMEOUT), check_cancel=check_job_cancelled)
        if duration > settings.MAX_VIDEO_SECONDS:
            raise ProcessingError(f'Максимальна тривалість відео — {settings.MAX_VIDEO_SECONDS} с.')
        if job.duration_seconds is None:
            current_attempt.update(duration_seconds=duration)
        if not captions.is_file():
            generate_subtitles(source, captions, budget=budget, language=job.subtitle_language)
        check_job_cancelled()
        save_progress(subtitles=captions_name)
        burn_subtitles(source, captions, output, budget=budget, subtitle_style=job.subtitle_style)
        budget.remaining_seconds(settings.JOB_TIMEOUT_SECONDS)
        save_progress(status=Job.Status.SUCCEEDED, stage='Готово', result=result_name, error='', finished_at=timezone.now())
        completed = True
    except ProcessingCancelled:
        cancelled = True
    except Exception as exc:
        logger.exception('Job %s failed', job.pk)
        if isinstance(exc, ValidationError):
            error = ' '.join(exc.messages)
        elif isinstance(exc, ProcessingError):
            error = str(exc)
        else:
            error = 'Помилка обробки. Спробуйте ще раз.'
        current_attempt.update(status=Job.Status.FAILED, stage='Помилка обробки', error=error, finished_at=timezone.now(), updated_at=timezone.now())
    finally:
        if not completed:
            output.unlink(missing_ok=True)
        if cancelled and not Job.objects.filter(pk=job.pk, subtitles=captions_name).exists():
            captions.unlink(missing_ok=True)


@shared_task(bind=True, name='subtitles.process_video', acks_late=True, reject_on_worker_lost=True)
def process_video_task(self, job_id, source_name, token):
    job = Job.objects.filter(
        pk=job_id,
        source=source_name,
        task_token=token,
        status=Job.Status.QUEUED,
    ).first()
    if job is None:
        return
    if job.node_id is not None and job.node.name != settings.WORKER_NODE_NAME:
        return
    process_job(job, token=token, node_name=self.request.hostname or 'worker-1')
