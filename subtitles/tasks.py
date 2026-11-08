from celery import shared_task
from django.conf import settings
from . import heartbeat  # Register worker lifecycle signals.

from .models import Job
from .services import process_job


@shared_task(bind=True, name='subtitles.process_video', acks_late=True, reject_on_worker_lost=True)
def process_video_task(self, job_id, source_name, token):
    job = Job.objects.filter(pk=job_id, source=source_name, task_token=token, status=Job.Status.QUEUED).first()
    if job is not None and (job.node_id is None or job.node.name == settings.WORKER_NODE_NAME):
        process_job(job, token=token, node_name=self.request.hostname or 'worker-1')
