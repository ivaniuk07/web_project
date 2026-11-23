from collections import defaultdict
from datetime import datetime, timedelta, timezone as datetime_timezone
from statistics import mean
import logging
import math
import uuid

from celery.signals import heartbeat_sent, worker_ready, worker_shutdown
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, transaction
from django.db.models import Count, F, Q
from django.utils import timezone
from kombu import Connection

from .models import Job, WorkerNode


def is_worker_available(node, now):
    if not node.enabled or node.heartbeat_at is None:
        return False

    heartbeat_deadline = now - timedelta(seconds=settings.NODE_TIMEOUT_SECONDS)
    if node.heartbeat_at < heartbeat_deadline:
        return False

    return node.unavailable_until is None or node.unavailable_until <= now


def select_worker(nodes, completion_times):
    oldest_time = datetime(1970, 1, 1, tzinfo=datetime_timezone.utc)

    def worker_priority(node):
        completion_seconds = completion_times.get(node.pk, 0)
        last_assignment = node.last_assigned_at or oldest_time
        return completion_seconds, last_assignment, node.pk

    return min(nodes, key=worker_priority)


class WorkloadForecast:
    def __init__(self, node_ids):
        self.node_ids = tuple(node_ids)
        history = Job.objects.filter(
            status=Job.Status.SUCCEEDED, node_id__in=self.node_ids,
            finished_at__gt=F('started_at'),
        ).order_by('-finished_at', '-pk').values_list('node_id', 'duration_seconds', 'started_at', 'finished_at')[:50]

        processing_times = defaultdict(list)
        video_samples = defaultdict(list)
        all_video_samples = []
        self.sample_count = len(history)
        for node_id, video_seconds, started_at, finished_at in history:
            processing_seconds = (finished_at - started_at).total_seconds()
            processing_times[node_id].append(processing_seconds)
            if video_seconds and video_seconds > 0:
                sample = (processing_seconds, video_seconds)
                video_samples[node_id].append(sample)
                all_video_samples.append(sample)

        self.average_times = {node_id: mean(times) for node_id, times in processing_times.items()}
        self.rates = {
            node_id: self.calculate_processing_rate(samples)
            for node_id, samples in video_samples.items()
        }
        self.default_rate = self.calculate_processing_rate(all_video_samples)

    @staticmethod
    def calculate_processing_rate(samples):
        if not samples:
            return 1.0
        total_processing_seconds = sum(sample[0] for sample in samples)
        total_video_seconds = sum(sample[1] for sample in samples)
        return total_processing_seconds / total_video_seconds

    def estimate_job_seconds(self, job, node_id):
        if job.duration_seconds and job.duration_seconds > 0:
            processing_rate = self.rates.get(node_id, self.default_rate)
            estimated_seconds = job.duration_seconds * processing_rate
            return max(1, estimated_seconds)
        return self.average_times.get(node_id, settings.ETA_FALLBACK_SECONDS)

    def get_worker_loads(self, now, *, include_queued=False):
        loads = dict.fromkeys(self.node_ids, 0.0)
        states = Q(status=Job.Status.RUNNING)
        if include_queued:
            states |= Q(status=Job.Status.QUEUED, task_token__isnull=False)
        active = Job.objects.filter(states, node_id__in=self.node_ids).only(
            'node_id', 'status', 'duration_seconds', 'started_at',
        ).order_by()
        for job in active:
            seconds = self.estimate_job_seconds(job, job.node_id)
            if job.status == Job.Status.RUNNING and job.started_at:
                elapsed = max(0, (now - job.started_at).total_seconds())
                seconds = max(1, seconds - elapsed)
            loads[job.node_id] += seconds
        return loads

    def estimate_completion_times(self, job, loads):
        completion_times = {}
        for node_id, queued_seconds in loads.items():
            new_job_seconds = self.estimate_job_seconds(job, node_id)
            completion_times[node_id] = queued_seconds + new_job_seconds
        return completion_times


def get_system_status():
    now = timezone.now()
    nodes = WorkerNode.objects.annotate(
        running_count=Count('job', filter=Q(job__status=Job.Status.RUNNING)),
        queued_count=Count('job', filter=Q(job__status=Job.Status.QUEUED, job__task_token__isnull=False)),
        succeeded_count=Count('job', filter=Q(job__status=Job.Status.SUCCEEDED)),
    ).order_by('name')
    waiting_jobs = Job.objects.filter(status=Job.Status.QUEUED, task_token__isnull=False)
    worker_statuses = []
    for node in nodes:
        worker_statuses.append({
            'name': node.name,
            'available': is_worker_available(node, now),
            'running': node.running_count,
            'queued': node.queued_count,
            'succeeded': node.succeeded_count,
            'heartbeat_at': node.heartbeat_at.isoformat() if node.heartbeat_at else None,
        })
    return {
        'nodes': worker_statuses,
        'waiting': waiting_jobs.count(),
        'pending': waiting_jobs.filter(node__isnull=True).count(),
        'running': Job.objects.filter(status=Job.Status.RUNNING).count(),
        'awaiting_submission': Job.objects.filter(status=Job.Status.UPLOADED).count(),
    }


def estimate_wait_time(job, now=None):
    if job.status != Job.Status.QUEUED or not job.task_token:
        return None
    now = now or timezone.now()
    nodes = [node for node in WorkerNode.objects.order_by('pk') if is_worker_available(node, now)]
    if not nodes:
        return {'seconds': None, 'sample_count': 0, 'label': 'Оцінка недоступна: немає доступних вузлів.'}
    forecast = WorkloadForecast(node.pk for node in nodes)
    workloads = forecast.get_worker_loads(now)
    queued = Job.objects.filter(status=Job.Status.QUEUED, task_token__isnull=False)
    for queued_job in queued.filter(node_id__in=workloads).order_by(F('dispatched_at').asc(nulls_last=True), 'created_at', 'pk'):
        if queued_job.pk == job.pk:
            return format_wait_estimate(workloads[queued_job.node_id], forecast.sample_count)
        workloads[queued_job.node_id] += forecast.estimate_job_seconds(queued_job, queued_job.node_id)
    for index, queued_job in enumerate(queued.filter(node__isnull=True).order_by('created_at', 'pk')):
        completion_times = forecast.estimate_completion_times(queued_job, workloads)
        node = select_worker(nodes, completion_times)
        node_id = node.pk
        if queued_job.pk == job.pk:
            return format_wait_estimate(workloads[node_id], forecast.sample_count)
        workloads[node_id] += forecast.estimate_job_seconds(queued_job, node_id)
        node.last_assigned_at = now + timedelta(microseconds=index)
    return {'seconds': None, 'sample_count': forecast.sample_count,
            'label': 'Оцінка недоступна: очікуємо перепризначення вузла.'}


def format_wait_estimate(wait, sample_count):
    seconds = math.ceil(wait)
    return {'seconds': seconds, 'sample_count': sample_count,
            'label': f'Орієнтовне очікування до початку: приблизно {seconds} с. Залежить від тривалості відео та навантаження.'}


logger = logging.getLogger(__name__)


def sync_worker_nodes():
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


def publish_job(node, job):
    from .tasks import process_video_task
    with Connection(node.broker_url, connect_timeout=2, transport_options={
        'socket_connect_timeout': 2, 'socket_timeout': 2,
    }) as connection:
        process_video_task.apply_async(
            args=[str(job.pk), job.source.name, str(job.task_token)],
            task_id=str(job.task_token), queue=node.queue,
            connection=connection, retry=False,
        )


def requeue_stalled_jobs():
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
        Job.objects.filter(pk=job.pk, task_token=job.task_token, status=job.status).update(
            status=Job.Status.QUEUED, task_token=uuid.uuid4(), node=None,
            node_name='', dispatched_at=None, published_at=None,
            started_at=None, finished_at=None,
            stage='Очікування повторного призначення вузла', updated_at=now,
        )


def dispatch_next_job(job_id=None):
    now = timezone.now()
    with transaction.atomic():
        locked_nodes = list(WorkerNode.objects.select_for_update().order_by('pk'))
        nodes = [node for node in locked_nodes if is_worker_available(node, now)]
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
        loads = forecast.get_worker_loads(now, include_queued=True)
        completion_times = forecast.estimate_completion_times(job, loads)
        node = select_worker(nodes, completion_times)
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

    pending = Job.objects.filter(pk=job.pk, task_token=job.task_token, status=Job.Status.QUEUED)
    try:
        publish_job(node, job)
    except Exception:
        logger.exception('Queue publication failed for job %s on node %s', job.pk, node.name)
        WorkerNode.objects.filter(pk=node.pk).update(unavailable_until=timezone.now() + timedelta(seconds=settings.NODE_RETRY_SECONDS))
        pending.update(
            node=None, node_name='', task_token=uuid.uuid4(),
            dispatched_at=None, published_at=None,
            stage='Черга недоступна. Очікування повторної спроби', updated_at=timezone.now(),
        )
    else:
        pending.update(published_at=timezone.now())

    return True


def update_heartbeat(**kwargs):
    close_old_connections()
    try:
        if not WorkerNode.objects.filter(name=settings.WORKER_NODE_NAME).exists():
            worker_config = None
            for node_config in settings.WORKER_NODES:
                if node_config['name'] == settings.WORKER_NODE_NAME:
                    worker_config = node_config
                    break
            if worker_config is None:
                return
            WorkerNode.objects.get_or_create(name=worker_config['name'], defaults={
                'broker_url': worker_config['broker_url'],
                'queue': worker_config['queue'],
                'enabled': worker_config.get('enabled', True),
            })
        WorkerNode.objects.filter(name=settings.WORKER_NODE_NAME).update(heartbeat_at=timezone.now())
    except Exception:
        logger.exception('Worker heartbeat could not be saved')


@worker_shutdown.connect
def unregister_worker(**kwargs):
    WorkerNode.objects.filter(name=settings.WORKER_NODE_NAME).update(heartbeat_at=None)


worker_ready.connect(update_heartbeat, weak=False)
heartbeat_sent.connect(update_heartbeat, weak=False)
