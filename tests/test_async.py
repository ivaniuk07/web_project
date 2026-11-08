import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from subtitles.models import Job, WorkerNode
from subtitles.services import enqueue_job, cancel_job, process_job


class AsyncJobTests(TestCase):
    def setUp(self):
        from django.utils import timezone
        WorkerNode.objects.create(name='node-1', broker_url='redis://localhost:6379/0', queue='subtitles-local', heartbeat_at=timezone.now())
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.root.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.user = get_user_model().objects.create_user('async-user')
        self.client.force_login(self.user)
        self.job = Job.objects.create(owner=self.user, original_name='clip.mp4', source='videos/clip.mp4')
        source = Path(self.job.source.path)
        source.parent.mkdir(parents=True)
        source.write_bytes(b'video')

    @patch('subtitles.tasks.process_video_task.apply_async')
    def test_enqueue_returns_without_processing_and_is_idempotent(self, publish):
        self.client.post(reverse('process_video', args=[self.job.pk]))
        self.client.post(reverse('process_video', args=[self.job.pk]))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.QUEUED)
        self.assertIsNotNone(self.job.task_token)
        self.assertEqual(publish.call_count, 1)

    @patch('subtitles.tasks.process_video_task.apply_async', side_effect=ConnectionError('offline'))
    def test_broker_failure_is_retryable(self, publish):
        response = self.client.post(reverse('process_video', args=[self.job.pk]), follow=True)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.QUEUED)
        self.assertContains(response, 'повторної спроби')

    @patch('subtitles.tasks.process_video_task.apply_async')
    def test_cancelled_queued_message_does_not_execute(self, publish):
        from subtitles.tasks import process_video_task
        enqueue_job(self.job)
        self.job.refresh_from_db()
        token = str(self.job.task_token)
        cancel_job(self.job)
        with patch('subtitles.services.generate_subtitles') as generate:
            process_video_task.run(str(self.job.pk), self.job.source.name, token)
        generate.assert_not_called()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.CANCELLED)

    def test_cancel_in_flight_cannot_publish_result(self):
        def generate(source, destination, **kwargs):
            cancel_job(self.job)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('captions', encoding='utf-8')
        with patch('subtitles.services.inspect_video', return_value=5), patch('subtitles.services.generate_subtitles', side_effect=generate), patch('subtitles.services.burn_subtitles') as burn:
            process_job(self.job)
        burn.assert_not_called()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.CANCELLED)
        self.assertFalse(self.job.result)

    def test_cancel_requires_owner_post_and_csrf(self):
        self.assertEqual(self.client.get(reverse('cancel_job', args=[self.job.pk])).status_code, 405)
        other = get_user_model().objects.create_user('async-other')
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse('cancel_job', args=[self.job.pk])).status_code, 404)

    def test_cancel_after_render_never_exposes_video(self):
        def generate(source, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('captions', encoding='utf-8')
        def render(source, captions, destination, **kwargs):
            cancel_job(self.job)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'late-video')
        with patch('subtitles.services.inspect_video', return_value=5), patch('subtitles.services.generate_subtitles', side_effect=generate), patch('subtitles.services.burn_subtitles', side_effect=render):
            process_job(self.job)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.CANCELLED)
        self.assertFalse(self.job.result)
        self.assertTrue(Path(self.job.source.path).is_file())
        self.assertFalse(list((Path(self.root.name) / 'rendered').glob('*.mp4')))
        self.assertEqual(self.client.get(reverse('download_subtitles', args=[self.job.pk])).status_code, 404)

    @patch('subtitles.tasks.process_video_task.apply_async')
    def test_enqueued_job_can_be_recovered_and_old_message_is_ignored(self, publish):
        from django.core.management import call_command
        from subtitles.tasks import process_video_task
        enqueue_job(self.job)
        self.job.refresh_from_db()
        old_token = str(self.job.task_token)
        call_command('recover_job', str(self.job.pk), verbosity=0)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.FAILED)
        self.assertIsNone(self.job.task_token)
        enqueue_job(self.job)
        self.job.refresh_from_db()
        self.assertNotEqual(str(self.job.task_token), old_token)
        with patch('subtitles.services.generate_subtitles') as generate:
            process_video_task.run(str(self.job.pk), self.job.source.name, old_token)
        generate.assert_not_called()
