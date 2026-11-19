import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone as datetime_timezone
from statistics import mean

from django.conf import settings
from django.db.models import Count, F, Q
from django.utils import timezone

from .models import Job, WorkerNode


def is_worker_available(node, now):
    if not node.enabled or node.heartbeat_at is None:
        return False

    heartbeat_deadline = now - timedelta(seconds=settings.NODE_TIMEOUT_SECONDS)
    if node.heartbeat_at < heartbeat_deadline:
        return False

    return node.unavailable_until is None or node.unavailable_until <= now


class WorkerBalancer:
    def select_worker(self, nodes, completion_times):
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
    # Assigned messages are already in their own FIFO broker, ahead of future assignments.
    for queued_job in queued.filter(node_id__in=workloads).order_by(F('dispatched_at').asc(nulls_last=True), 'created_at', 'pk'):
        if queued_job.pk == job.pk:
            return format_wait_estimate(workloads[queued_job.node_id], forecast.sample_count)
        workloads[queued_job.node_id] += forecast.estimate_job_seconds(queued_job, queued_job.node_id)
    # Unassigned work is dispatched by creation time; simulate its turn across available nodes.
    balancer = WorkerBalancer()
    for index, queued_job in enumerate(queued.filter(node__isnull=True).order_by('created_at', 'pk')):
        completion_times = forecast.estimate_completion_times(queued_job, workloads)
        node = balancer.select_worker(nodes, completion_times)
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
