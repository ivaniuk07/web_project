import uuid
from pathlib import Path

from django.conf import settings
from django.db import models

from .languages import SubtitleLanguage
from .subtitle_styles import SubtitleStyle


def video_upload_path(job, filename):
    return f'videos/{job.pk}{Path(filename).suffix.lower()}'


class WorkerNode(models.Model):
    name = models.CharField(max_length=100, unique=True)
    broker_url = models.CharField(max_length=512)
    queue = models.CharField(max_length=100, default='subtitles')
    enabled = models.BooleanField(default=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    unavailable_until = models.DateTimeField(null=True, blank=True)
    last_assigned_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name


class Job(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'queued', 'Очікує обробки'
        RUNNING = 'running', 'Обробляється'
        SUCCEEDED = 'succeeded', 'Завершено'
        FAILED = 'failed', 'Помилка'
        CANCELLED = 'cancelled', 'Скасовано'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='jobs')
    original_name = models.CharField(max_length=255)
    duration_seconds = models.FloatField(null=True, blank=True)
    source = models.FileField(upload_to=video_upload_path, unique=True, max_length=512)
    subtitles = models.FileField(upload_to='subtitles/', blank=True, max_length=512)
    subtitle_style = models.CharField(max_length=16, choices=SubtitleStyle.choices, default=SubtitleStyle.CLASSIC)
    subtitle_language = models.CharField(max_length=2, choices=SubtitleLanguage.choices, blank=True, default=SubtitleLanguage.AUTO)
    result = models.FileField(upload_to='rendered/', blank=True, max_length=512)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    node_name = models.CharField(max_length=100, blank=True)
    task_token = models.UUIDField(null=True, blank=True)
    stage = models.CharField(max_length=100, default='Очікує запуску')
    node = models.ForeignKey(WorkerNode, null=True, blank=True, on_delete=models.SET_NULL)
    dispatch_attempts = models.PositiveIntegerField(default=0)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.original_name
