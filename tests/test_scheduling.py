from datetime import timedelta
from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from subtitles.models import Job, WorkerNode
from subtitles.scheduling import Dispatcher


class SchedulingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('scheduler')
        self.first = WorkerNode.objects.create(name='node-1', broker_url='redis://localhost:6379/0', queue='subtitles-local', heartbeat_at=timezone.now())
        self.second = WorkerNode.objects.create(name='node-2', broker_url='redis://localhost:6380/0', queue='subtitles-local', heartbeat_at=timezone.now())
        self.router = Mock()
        self.dispatcher = Dispatcher(router=self.router)

    def job(self, **values):
        import uuid
        return Job.objects.create(owner=self.user, source='videos/' + str(uuid.uuid4()) + '.mp4', original_name='clip.mp4', task_token=uuid.uuid4(), **values)

    def test_jobs_choose_different_nodes_and_store_route(self):
        a, b = self.job(), self.job()
        self.dispatcher.dispatch_once()
        self.dispatcher.dispatch_once()
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.node, self.first)
        self.assertEqual(b.node, self.second)
        self.assertEqual(a.node_name, 'node-1')
        self.assertIsNotNone(a.published_at)

    def test_equal_load_uses_least_recently_assigned_node(self):
        self.first.last_assigned_at = timezone.now()
        self.first.save()
        job = self.job()
        self.dispatcher.dispatch_once()
        job.refresh_from_db()
        self.assertEqual(job.node, self.second)

    def test_two_short_jobs_leave_less_work_than_one_long_job(self):
        self.job(node=self.first, duration_seconds=600, status=Job.Status.RUNNING, started_at=timezone.now())
        for _ in range(2):
            self.job(node=self.second, duration_seconds=20)
        target = self.job(duration_seconds=30)

        self.dispatcher.dispatch_once(job_id=target.pk)

        target.refresh_from_db()
        self.assertEqual(target.node, self.second)

    def test_faster_worker_can_finish_first_even_with_a_queue(self):
        finished_at = timezone.now()
        for node, processing_seconds in ((self.first, 30), (self.second, 120)):
            self.job(node=node, status=Job.Status.SUCCEEDED, duration_seconds=60,
                     started_at=finished_at - timedelta(seconds=processing_seconds), finished_at=finished_at)
        self.job(node=self.first, duration_seconds=20)
        target = self.job(duration_seconds=60)

        self.dispatcher.dispatch_once(job_id=target.pk)

        target.refresh_from_db()
        self.assertEqual(target.node, self.first)

    def test_no_nodes_keeps_job_waiting(self):
        WorkerNode.objects.update(heartbeat_at=None)
        job = self.job()
        self.assertFalse(self.dispatcher.dispatch_once())
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertIsNone(job.node_id)
        self.router.publish.assert_not_called()

    def test_failed_publish_retries_on_other_node(self):
        self.router.publish.side_effect = [ConnectionError('offline'), None]
        job = self.job()
        old_token = job.task_token
        self.dispatcher.dispatch_once()
        self.dispatcher.dispatch_once()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertEqual(job.node, self.second)
        self.assertNotEqual(old_token, job.task_token)
        self.assertIsNotNone(job.published_at)

    def test_stale_node_reassigns_running_job_and_invalidates_message(self):
        job = self.job()
        self.dispatcher.dispatch_once()
        job.refresh_from_db()
        old_token = job.task_token
        Job.objects.filter(pk=job.pk).update(status=Job.Status.RUNNING)
        WorkerNode.objects.filter(pk=self.first.pk).update(heartbeat_at=timezone.now() - timedelta(hours=1))
        self.dispatcher.recover()
        self.dispatcher.dispatch_once()
        job.refresh_from_db()
        self.assertEqual(job.node, self.second)
        self.assertNotEqual(job.task_token, old_token)

    def test_third_node_is_selected_without_code_changes(self):
        self.first.enabled = False; self.first.save()
        self.second.enabled = False; self.second.save()
        third = WorkerNode.objects.create(name='node-3', broker_url='redis://localhost:6381/0', heartbeat_at=timezone.now())
        job = self.job()
        self.dispatcher.dispatch_once()
        job.refresh_from_db()
        self.assertEqual(job.node, third)

    def test_published_backlog_is_not_resubmitted(self):
        job = self.job()
        self.dispatcher.dispatch_once()
        Job.objects.filter(pk=job.pk).update(dispatched_at=timezone.now() - timedelta(hours=1))
        self.dispatcher.recover()
        self.assertFalse(self.dispatcher.dispatch_once())
        self.assertEqual(self.router.publish.call_count, 1)

    @override_settings(MAX_DISPATCH_ATTEMPTS=1)
    def test_retries_are_bounded(self):
        job = self.job()
        Job.objects.filter(pk=job.pk).update(dispatch_attempts=1)
        self.dispatcher.dispatch_once()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIsNone(job.task_token)
        self.assertIsNotNone(job.finished_at)

    def test_unpublished_reservation_is_retried(self):
        job = self.job()
        Job.objects.filter(pk=job.pk).update(node=self.first, dispatched_at=timezone.now() - timedelta(minutes=1))
        old_token = job.task_token
        self.dispatcher.recover()
        job.refresh_from_db()
        self.assertIsNone(job.node_id)
        self.assertNotEqual(old_token, job.task_token)

    def test_cancelled_job_is_never_recovered(self):
        job = self.job()
        Job.objects.filter(pk=job.pk).update(status=Job.Status.CANCELLED, node=self.first)
        WorkerNode.objects.filter(pk=self.first.pk).update(enabled=False)
        self.dispatcher.recover()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.CANCELLED)


    @override_settings(JOB_TIMEOUT_SECONDS=7200)
    def test_stale_running_child_recovers_with_live_parent(self):
        job = self.job()
        self.dispatcher.dispatch_once()
        job.refresh_from_db()
        old_token = job.task_token
        Job.objects.filter(pk=job.pk).update(status=Job.Status.RUNNING, updated_at=timezone.now() - timedelta(hours=1))
        self.dispatcher.recover()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.RUNNING)
        self.assertEqual(job.task_token, old_token)

        Job.objects.filter(pk=job.pk).update(updated_at=timezone.now() - timedelta(hours=3))
        self.dispatcher.recover()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertNotEqual(job.task_token, old_token)

    def test_rerouted_job_keeps_referenced_srt(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from subtitles.services import process_job
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root):
            job = self.job()
            source = Path(job.source.path)
            source.parent.mkdir(parents=True)
            source.write_bytes(b'video')
            self.dispatcher.dispatch_once()
            job.refresh_from_db()
            def generate(source, destination, **kwargs):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text('captions', encoding='utf-8')
            def burn(source, captions, destination, **kwargs):
                WorkerNode.objects.filter(pk=self.first.pk).update(heartbeat_at=None)
                self.dispatcher.recover()
                self.dispatcher.dispatch_once()
            with patch('subtitles.services.inspect_video', return_value=5), patch('subtitles.services.generate_subtitles', side_effect=generate), patch('subtitles.services.burn_subtitles', side_effect=burn):
                process_job(job, token=job.task_token)
            job.refresh_from_db()
            self.assertEqual(job.status, Job.Status.QUEUED)
            self.assertEqual(job.node, self.second)
            self.assertTrue(Path(job.subtitles.path).is_file())


    def test_invalid_configuration_does_not_partially_update_registry(self):
        from django.core.exceptions import ImproperlyConfigured
        from subtitles.scheduling import sync_nodes
        configs = [
            {'name': 'node-1', 'broker_url': 'redis://changed:6379/0', 'queue': 'same'},
            {'name': 'node-2', 'broker_url': 'redis://changed:6379/0', 'queue': 'same'},
        ]
        with override_settings(WORKER_NODES=configs), self.assertRaises(ImproperlyConfigured):
            sync_nodes()
        self.first.refresh_from_db()
        self.assertEqual(self.first.broker_url, 'redis://localhost:6379/0')


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import close_old_connections
from django.test import TransactionTestCase


class ConcurrentDispatchTests(TransactionTestCase):
    def test_two_dispatchers_do_not_assign_same_job_twice(self):
        import uuid
        user = get_user_model().objects.create_user('parallel-dispatch')
        for name in ('node-1', 'node-2'):
            WorkerNode.objects.create(name=name, broker_url='redis://localhost:6379/0', heartbeat_at=timezone.now())
        for number in range(2):
            Job.objects.create(owner=user, source=f'videos/{number}.mp4', original_name='clip.mp4', task_token=uuid.uuid4())
        barrier = Barrier(2)
        def dispatch(_):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return Dispatcher(router=Mock()).dispatch_once()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(dispatch, [1, 2])), [True, True])
        jobs = list(Job.objects.order_by('created_at'))
        self.assertNotEqual(jobs[0].node_id, jobs[1].node_id)
        self.assertTrue(all(job.dispatch_attempts == 1 for job in jobs))
