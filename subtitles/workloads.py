from collections import defaultdict
from datetime import timedelta
from statistics import mean

from django.conf import settings
from django.db.models import F, Q

from .models import Job


def node_available(node, now):
    return bool(
        node.enabled and node.heartbeat_at
        and node.heartbeat_at >= now - timedelta(seconds=settings.NODE_TIMEOUT_SECONDS)
        and (node.unavailable_until is None or node.unavailable_until <= now)
    )


class WorkloadForecast:
    def __init__(self, node_ids):
        self.node_ids = tuple(node_ids)
        history = Job.objects.filter(
            status=Job.Status.SUCCEEDED, node_id__in=self.node_ids,
            finished_at__gt=F('started_at'),
        ).order_by('-finished_at', '-pk').values_list('node_id', 'duration_seconds', 'started_at', 'finished_at')[:50]

        samples = defaultdict(list)
        known = defaultdict(list)
        for node_id, duration, started, finished in history:
            seconds = (finished - started).total_seconds()
            samples[node_id].append(seconds)
            if duration and duration > 0:
                known[node_id].append((seconds, duration))

        self.sample_count = sum(map(len, samples.values()))
        self.average_times = {node_id: mean(times) for node_id, times in samples.items()}
        self.rates = {node_id: self._rate(times) for node_id, times in known.items()}
        self.default_rate = self._rate([sample for times in known.values() for sample in times])

    @staticmethod
    def _rate(samples):
        if samples:
            return sum(seconds for seconds, _ in samples) / sum(duration for _, duration in samples)
        return 1.0

    def job_seconds(self, job, node_id):
        if job.duration_seconds and job.duration_seconds > 0:
            return max(1, job.duration_seconds * self.rates.get(node_id, self.default_rate))
        return self.average_times.get(node_id, settings.ETA_FALLBACK_SECONDS)

    def loads(self, now, *, include_queued=False):
        loads = dict.fromkeys(self.node_ids, 0.0)
        states = Q(status=Job.Status.RUNNING)
        if include_queued:
            states |= Q(status=Job.Status.QUEUED, task_token__isnull=False)
        active = Job.objects.filter(states, node_id__in=self.node_ids).only(
            'node_id', 'status', 'duration_seconds', 'started_at',
        ).order_by()
        for job in active:
            seconds = self.job_seconds(job, job.node_id)
            if job.status == Job.Status.RUNNING and job.started_at:
                elapsed = max(0, (now - job.started_at).total_seconds())
                seconds = max(1, seconds - elapsed)
            loads[job.node_id] += seconds
        return loads

    def completion_times(self, job, loads):
        return {node_id: seconds + self.job_seconds(job, node_id) for node_id, seconds in loads.items()}
