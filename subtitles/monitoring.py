"""Read-only capacity and approximate waiting time, independent of HTTP and routing."""
import math
from datetime import timedelta

from django.db.models import Count, F, Q
from django.utils import timezone

from .models import Job, WorkerNode
from .scheduling import LeastLoadedBalancer
from .workloads import WorkloadForecast, node_available


def capacity_snapshot():
    now = timezone.now()
    nodes = WorkerNode.objects.annotate(
        running_count=Count('job', filter=Q(job__status=Job.Status.RUNNING)),
        queued_count=Count('job', filter=Q(job__status=Job.Status.QUEUED, job__task_token__isnull=False)),
        succeeded_count=Count('job', filter=Q(job__status=Job.Status.SUCCEEDED)),
    ).order_by('name')
    waiting = Job.objects.filter(status=Job.Status.QUEUED, task_token__isnull=False)
    return {
        'nodes': [{'name': node.name, 'available': node_available(node, now),
                   'running': node.running_count, 'queued': node.queued_count,
                   'succeeded': node.succeeded_count,
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
    forecast = WorkloadForecast(node.pk for node in nodes)
    workloads = forecast.loads(now)
    queued = Job.objects.filter(status=Job.Status.QUEUED, task_token__isnull=False)
    # Assigned messages are already in their own FIFO broker, ahead of future assignments.
    for queued_job in queued.filter(node_id__in=workloads).order_by(F('dispatched_at').asc(nulls_last=True), 'created_at', 'pk'):
        if queued_job.pk == job.pk:
            return _estimate(workloads[queued_job.node_id], forecast.sample_count)
        workloads[queued_job.node_id] += forecast.job_seconds(queued_job, queued_job.node_id)
    # Unassigned work is dispatched by creation time; simulate its turn across available nodes.
    balancer = LeastLoadedBalancer()
    for index, queued_job in enumerate(queued.filter(node__isnull=True).order_by('created_at', 'pk')):
        node = balancer.select(nodes, forecast.completion_times(queued_job, workloads))
        node_id = node.pk
        if queued_job.pk == job.pk:
            return _estimate(workloads[node_id], forecast.sample_count)
        workloads[node_id] += forecast.job_seconds(queued_job, node_id)
        node.last_assigned_at = now + timedelta(microseconds=index)
    return {'seconds': None, 'sample_count': forecast.sample_count,
            'label': 'Оцінка недоступна: очікуємо перепризначення вузла.'}


def _estimate(wait, sample_count):
    seconds = math.ceil(wait)
    return {'seconds': seconds, 'sample_count': sample_count,
            'label': f'Орієнтовне очікування до початку: приблизно {seconds} с. Залежить від тривалості відео та навантаження.'}
