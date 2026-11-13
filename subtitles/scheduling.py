"""Compute selection, Redis routing and durable retry coordination."""
import logging
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone
from typing import Protocol

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from kombu import Connection

from .models import Job, WorkerNode
from .workloads import WorkloadForecast, node_available

logger = logging.getLogger(__name__)


def sync_nodes():
    names = set()
    routes = set()
    for config in settings.WORKER_NODES:
        name = config.get('name')
        route = (config.get('broker_url'), config.get('queue'))
        if not all(isinstance(value, str) and value for value in (name, *route)) or name in names or route in routes:
            raise ImproperlyConfigured('WORKER_NODES requires unique names and distinct broker/queue pairs.')
        names.add(name)
        routes.add(route)
    with transaction.atomic():
        list(WorkerNode.objects.select_for_update().order_by('pk'))
        for config in sorted(settings.WORKER_NODES, key=lambda node: node['name']):
            WorkerNode.objects.update_or_create(name=config['name'], defaults={
                'broker_url': config['broker_url'], 'queue': config['queue'],
                'enabled': config.get('enabled', True),
            })
        WorkerNode.objects.exclude(name__in=names).update(enabled=False)


class TaskRouter(Protocol):
    def publish(self, node: WorkerNode, job: Job) -> None: ...


class RedisQueueRouter:
    def publish(self, node, job):
        from .tasks import process_video_task
        with Connection(node.broker_url, connect_timeout=2, transport_options={
            'socket_connect_timeout': 2, 'socket_timeout': 2,
        }) as connection:
            process_video_task.apply_async(
                args=[str(job.pk), job.source.name, str(job.task_token)],
                task_id=str(job.task_token), queue=node.queue,
                connection=connection, retry=False,
            )


class LeastLoadedBalancer:
    def select(self, nodes, loads):
        epoch = datetime(1970, 1, 1, tzinfo=datetime_timezone.utc)
        return min(nodes, key=lambda node: (
            loads.get(node.pk, 0), node.last_assigned_at or epoch, node.pk,
        ))


class Dispatcher:
    def __init__(self, router=None, balancer=None):
        self.router = router or RedisQueueRouter()
        self.balancer = balancer or LeastLoadedBalancer()

    def recover(self):
        now = timezone.now()
        cutoff = now - timedelta(seconds=settings.NODE_TIMEOUT_SECONDS)
        unavailable = WorkerNode.objects.filter(Q(enabled=False) | Q(heartbeat_at__isnull=True) | Q(heartbeat_at__lt=cutoff))
        reserved_cutoff = now - timedelta(seconds=settings.DISPATCH_RESERVATION_SECONDS)
        stale = Job.objects.filter(status__in=[Job.Status.QUEUED, Job.Status.RUNNING], task_token__isnull=False).filter(
            Q(node__in=unavailable)
            | Q(status=Job.Status.QUEUED, node__isnull=False, published_at__isnull=True, dispatched_at__lt=reserved_cutoff)
            | Q(status=Job.Status.RUNNING, updated_at__lt=now - timedelta(seconds=settings.JOB_TIMEOUT_SECONDS + 60))
        )
        for job in stale:
            # Conditional update fences completion and cancellation racing recovery.
            Job.objects.filter(pk=job.pk, task_token=job.task_token, status=job.status).update(
                status=Job.Status.QUEUED, task_token=uuid.uuid4(), node=None,
                node_name='', dispatched_at=None, published_at=None,
                started_at=None, finished_at=None,
                stage='Очікування повторного призначення вузла', updated_at=now,
            )

    def dispatch_once(self, job_id=None):
        now = timezone.now()
        with transaction.atomic():
            # Shared stable lock order serializes multiple dispatcher processes.
            locked_nodes = list(WorkerNode.objects.select_for_update().order_by('pk'))
            nodes = [node for node in locked_nodes if node_available(node, now)]
            pending = Job.objects.select_for_update().filter(
                status=Job.Status.QUEUED, task_token__isnull=False, node__isnull=True,
            ).order_by('created_at', 'pk')
            if job_id is not None:
                pending = pending.filter(pk=job_id)
            job = pending.first()
            if job is None:
                return False
            if not nodes:
                job.stage = 'Очікування доступного вузла'
                job.save(update_fields=['stage', 'updated_at'])
                return False
            if job.dispatch_attempts >= settings.MAX_DISPATCH_ATTEMPTS:
                job.status = Job.Status.FAILED
                job.error = 'Вичерпано кількість спроб передачі до черги. Запустіть задачу повторно.'
                job.stage = 'Помилка передачі'
                job.task_token = None
                job.finished_at = now
                job.save(update_fields=['status', 'error', 'stage', 'task_token', 'finished_at', 'updated_at'])
                return True
            forecast = WorkloadForecast(node.pk for node in nodes)
            loads = forecast.loads(now, include_queued=True)
            node = self.balancer.select(nodes, forecast.completion_times(job, loads))
            job.node = node
            job.node_name = node.name
            job.task_token = uuid.uuid4()
            job.dispatch_attempts += 1
            job.dispatched_at = now
            job.published_at = None
            job.stage = f'У черзі вузла {node.name}'
            job.save(update_fields=['node', 'node_name', 'task_token', 'dispatch_attempts', 'dispatched_at', 'published_at', 'stage', 'updated_at'])
            node.last_assigned_at = now
            node.save(update_fields=['last_assigned_at'])
        try:
            self.router.publish(node, job)
        except Exception:
            logger.exception('Queue publication failed for job %s on node %s', job.pk, node.name)
            WorkerNode.objects.filter(pk=node.pk).update(unavailable_until=timezone.now() + timedelta(seconds=settings.NODE_RETRY_SECONDS))
            Job.objects.filter(pk=job.pk, task_token=job.task_token, status=Job.Status.QUEUED).update(
                node=None, node_name='', task_token=uuid.uuid4(),
                dispatched_at=None, published_at=None,
                stage='Черга недоступна. Очікування повторної спроби', updated_at=timezone.now(),
            )
        else:
            Job.objects.filter(pk=job.pk, task_token=job.task_token, status=Job.Status.QUEUED).update(published_at=timezone.now())
        return True
