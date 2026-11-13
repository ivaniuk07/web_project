import uuid
from datetime import timedelta
from importlib.util import find_spec

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from subtitles.models import Job, WorkerNode


@override_settings(ETA_FALLBACK_SECONDS=60)
class WaitingEstimateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('eta-user')
        self.now = timezone.now()
        self.nodes = [WorkerNode.objects.create(name=f'node-{i}', broker_url=f'redis://localhost:{6378+i}/0', heartbeat_at=self.now) for i in (1, 2)]

    def job(self, **values):
        return Job.objects.create(owner=self.user, original_name='clip.mp4', source=f'videos/{uuid.uuid4()}.mp4', **values)

    def estimate(self, job):
        self.assertIsNotNone(find_spec('subtitles.monitoring'), 'Missing independent ETA service')
        from subtitles.monitoring import estimate_wait
        return estimate_wait(job, now=self.now)

    def busy_pair(self):
        self.assertIn('started_at', {field.name for field in Job._meta.fields})
        for node in self.nodes:
            self.job(status='running', node=node, node_name=node.name, started_at=self.now, task_token=uuid.uuid4())

    def test_busy_pair_uses_labelled_fallback(self):
        self.busy_pair()
        estimate = self.estimate(self.job(task_token=uuid.uuid4()))
        self.assertEqual(estimate['seconds'], 60)
        self.assertIn('приблизно', estimate['label'].lower())
        self.assertEqual(estimate['sample_count'], 0)

    def test_completed_samples_and_elapsed_time_reduce_wait(self):
        self.busy_pair()
        for node in self.nodes:
            self.job(status='succeeded', node=node, started_at=self.now-timedelta(seconds=200), finished_at=self.now-timedelta(seconds=100))
        Job.objects.filter(status='running').update(started_at=self.now-timedelta(seconds=30))
        estimate = self.estimate(self.job(task_token=uuid.uuid4()))
        self.assertEqual(estimate['seconds'], 70)
        self.assertEqual(estimate['sample_count'], 2)

    def test_assigned_predecessor_adds_one_service_time(self):
        self.busy_pair()
        node = self.nodes[0]
        self.job(node=node, task_token=uuid.uuid4(), dispatched_at=self.now-timedelta(seconds=2))
        target = self.job(node=node, task_token=uuid.uuid4(), dispatched_at=self.now-timedelta(seconds=1))
        self.assertEqual(self.estimate(target)['seconds'], 120)

    def test_unavailable_and_cooldown_nodes_cannot_give_zero_wait(self):
        WorkerNode.objects.filter(pk=self.nodes[0].pk).update(heartbeat_at=self.now-timedelta(minutes=2))
        WorkerNode.objects.filter(pk=self.nodes[1].pk).update(unavailable_until=self.now+timedelta(minutes=1))
        estimate = self.estimate(self.job(task_token=uuid.uuid4()))
        self.assertIsNone(estimate['seconds'])
        self.assertIn('недоступ', estimate['label'].lower())

    def test_unsubmitted_and_terminal_jobs_have_no_estimate(self):
        self.assertIsNone(self.estimate(self.job()))
        self.assertIsNone(self.estimate(self.job(status='succeeded', task_token=uuid.uuid4())))

    def test_detail_shows_estimate_to_owner(self):
        self.busy_pair()
        job = self.job(task_token=uuid.uuid4())
        self.client.force_login(self.user)
        response = self.client.get(reverse('job_detail', args=[job.pk]))
        self.assertContains(response, 'приблизно')
        stranger = get_user_model().objects.create_user('eta-stranger')
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse('job_detail', args=[job.pk])).status_code, 404)

    def test_unassigned_estimate_follows_the_worker_predicted_to_finish_first(self):
        self.busy_pair()
        for node, duration in zip(self.nodes, [300, 60]):
            self.job(status='succeeded', node=node, started_at=self.now-timedelta(seconds=500), finished_at=self.now-timedelta(seconds=500-duration))
        WorkerNode.objects.filter(pk=self.nodes[0].pk).update(last_assigned_at=self.now-timedelta(minutes=2))
        WorkerNode.objects.filter(pk=self.nodes[1].pk).update(last_assigned_at=self.now-timedelta(minutes=1))
        self.assertEqual(self.estimate(self.job(task_token=uuid.uuid4()))['seconds'], 60)
