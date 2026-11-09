"""Read-only capacity and approximate waiting time, independent of HTTP and routing."""
import math
from collections import defaultdict
from datetime import timedelta
from statistics import mean

from django.conf import settings
from django.db.models import Count, F, Q
from django.utils import timezone

from .models import Job, WorkerNode
from .scheduling import LeastLoadedBalancer


def node_available(node, now):
    return bool(node.enabled and node.heartbeat_at
                and node.heartbeat_at >= now-timedelta(seconds=settings.NODE_TIMEOUT_SECONDS)
                and (node.unavailable_until is None or node.unavailable_until <= now))


def capacity_snapshot():
    now = timezone.now()
    nodes = WorkerNode.objects.annotate(
        running_count=Count('job', filter=Q(job__status=Job.Status.RUNNING)),
        queued_count=Count('job', filter=Q(job__status=Job.Status.QUEUED, job__task_token__isnull=False)),
    ).order_by('name')
    waiting = Job.objects.filter(status=Job.Status.QUEUED, task_token__isnull=False)
    return {
        'nodes': [{'name': node.name, 'available': node_available(node, now),
                   'running': node.running_count, 'queued': node.queued_count,
                   'heartbeat_at': node.heartbeat_at.isoformat() if node.heartbeat_at else None}
                  for node in nodes],
        'waiting': waiting.count(), 'pending': waiting.filter(node__isnull=True).count(),
        'running': Job.objects.filter(status=Job.Status.RUNNING).count(),
        'awaiting_submission': Job.objects.filter(status=Job.Status.QUEUED, task_token__isnull=True).count(),
    }


def estimate_wait(job, now=None):
    if job.status != Job.Status.QUEUED or not job.task_token:
        return None
    now = now or timezone.now()
    nodes = [node for node in WorkerNode.objects.order_by('pk') if node_available(node, now)]
    if not nodes:
        return {'seconds': None, 'sample_count': 0, 'label': 'Оцінка недоступна: немає доступних вузлів.'}
    samples = defaultdict(list)
    completed = Job.objects.filter(status=Job.Status.SUCCEEDED, started_at__isnull=False,
                                   finished_at__isnull=False, node_id__in=[n.pk for n in nodes]).order_by('-finished_at')[:50]
    for previous in completed:
        seconds = (previous.finished_at-previous.started_at).total_seconds()
        if seconds > 0:
            samples[previous.node_id].append(seconds)
    duration = {n.pk: mean(samples[n.pk]) if samples[n.pk] else settings.ETA_FALLBACK_SECONDS for n in nodes}
    workloads = {n.pk: 0.0 for n in nodes}
    counts = {n.pk: 0 for n in nodes}
    for running in Job.objects.filter(status=Job.Status.RUNNING, node_id__in=workloads):
        elapsed = max(0, (now-running.started_at).total_seconds()) if running.started_at else 0
        workloads[running.node_id] += max(1, duration[running.node_id]-elapsed)
        counts[running.node_id] += 1
    queued = Job.objects.filter(status=Job.Status.QUEUED, task_token__isnull=False)
    # Assigned messages are already in their own FIFO broker, ahead of future assignments.
    for queued_job in queued.filter(node_id__in=workloads).order_by(F('dispatched_at').asc(nulls_last=True), 'created_at', 'pk'):
        if queued_job.pk == job.pk:
            return _estimate(workloads[queued_job.node_id], samples)
        workloads[queued_job.node_id] += duration[queued_job.node_id]
        counts[queued_job.node_id] += 1
    # Unassigned work is dispatched by creation time; simulate its turn across available nodes.
    balancer = LeastLoadedBalancer()
    for index, queued_job in enumerate(queued.filter(node__isnull=True).order_by('created_at', 'pk')):
        node = balancer.select(nodes, counts)
        node_id = node.pk
        if queued_job.pk == job.pk:
            return _estimate(workloads[node_id], samples)
        workloads[node_id] += duration[node_id]
        counts[node_id] += 1
        node.last_assigned_at = now + timedelta(microseconds=index)
    return {'seconds': None, 'sample_count': sum(map(len, samples.values())),
            'label': 'Оцінка недоступна: очікуємо перепризначення вузла.'}


def _estimate(wait, samples):
    seconds = math.ceil(wait)
    return {'seconds': seconds, 'sample_count': sum(map(len, samples.values())),
            'label': f'Орієнтовне очікування до початку: приблизно {seconds} с. Залежить від тривалості відео та навантаження.'}
