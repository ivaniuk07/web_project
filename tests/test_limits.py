import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, SimpleTestCase, override_settings

from subtitles.models import Job
from subtitles.services import create_job, process_job


class AdmissionTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.directory.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.user = get_user_model().objects.create_user('limits')

    def video(self):
        return SimpleUploadedFile('clip.mp4', b'valid-video')

    @override_settings(MAX_UPLOAD_BYTES=2)
    def test_size_limit_refuses_before_saving(self):
        with self.assertRaises(ValidationError):
            create_job(self.user, self.video())
        self.assertFalse(Job.objects.exists())
        self.assertFalse(list(Path(self.directory.name).rglob('*.mp4')))

    @override_settings(ALLOWED_VIDEO_EXTENSIONS=['webm'])
    def test_configured_formats_apply_to_service(self):
        with self.assertRaises(ValidationError):
            create_job(self.user, self.video())

    @override_settings(MAX_VIDEO_SECONDS=10)
    @patch('subtitles.services.inspect_video', return_value=11)
    def test_long_video_is_removed_and_never_queued(self, inspect):
        with self.assertRaises(ValidationError):
            create_job(self.user, self.video())
        self.assertFalse(Job.objects.exists())
        self.assertFalse(list(Path(self.directory.name).rglob('*.mp4')))


    @override_settings(MAX_UPLOAD_BYTES=11, MAX_VIDEO_SECONDS=10, MAX_ACTIVE_JOBS_PER_USER=1)
    @patch('subtitles.services.inspect_video', return_value=10)
    def test_exact_limits_and_another_users_jobs_are_allowed(self, inspect):
        other = get_user_model().objects.create_user('other-limits')
        Job.objects.create(owner=other, source='videos/other.mp4', original_name='other.mp4')
        job = create_job(self.user, self.video())
        self.assertEqual(job.status, Job.Status.QUEUED)

    @override_settings(MAX_ACTIVE_JOBS_PER_USER=1)
    @patch('subtitles.services.inspect_video', return_value=5)
    def test_active_limit_and_completed_jobs(self, inspect):
        job = create_job(self.user, self.video())
        with self.assertRaises(ValidationError):
            create_job(self.user, self.video())
        Job.objects.filter(pk=job.pk).update(status=Job.Status.SUCCEEDED)
        self.assertIsNotNone(create_job(self.user, self.video()).pk)
        self.assertEqual(len(list(Path(self.directory.name).rglob('*.mp4'))), 2)

    @patch('subtitles.services.inspect_video', side_effect=ValidationError('Пошкоджене відео.'))
    def test_corrupt_video_is_not_queued(self, inspect):
        with self.assertRaises(ValidationError):
            create_job(self.user, self.video())
        self.assertFalse(Job.objects.exists())

    @override_settings(JOB_TIMEOUT_SECONDS=0)
    @patch('subtitles.services.generate_subtitles')
    def test_total_timeout_sets_failed_without_processing(self, generate):
        job = Job.objects.create(owner=self.user, source='videos/clip.mp4', original_name='clip.mp4')
        process_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn('час', job.error)
        generate.assert_not_called()


    @override_settings(MAX_ACTIVE_JOBS_PER_USER=1)
    @patch('subtitles.services.inspect_video', return_value=5)
    def test_accepted_backlog_can_drain_after_limit_is_lowered(self, inspect):
        job = Job.objects.create(owner=self.user, source='videos/clip.mp4', original_name='clip.mp4')
        source = Path(job.source.path)
        source.parent.mkdir(parents=True)
        source.write_bytes(b'video')
        Job.objects.create(owner=self.user, source='videos/second.mp4', original_name='second.mp4')
        def generate(source, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('captions', encoding='utf-8')
        def burn(source, captions, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'mp4')
        with patch('subtitles.services.generate_subtitles', side_effect=generate), patch('subtitles.services.burn_subtitles', side_effect=burn):
            process_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.SUCCEEDED)

    @override_settings(MAX_ACTIVE_JOBS_PER_USER=1)
    def test_failed_retry_does_not_bypass_active_limit(self):
        Job.objects.create(owner=self.user, source='videos/active.mp4', original_name='active.mp4')
        failed = Job.objects.create(owner=self.user, source='videos/failed.mp4', original_name='failed.mp4', status=Job.Status.FAILED)
        with self.assertRaises(ValidationError):
            process_job(failed)
        failed.refresh_from_db()
        self.assertEqual(failed.status, Job.Status.FAILED)


class MediaValidationTests(SimpleTestCase):
    @patch('subtitles.validation._run')
    def test_valid_duration_at_boundary(self, run):
        from subtitles.validation import inspect_video
        run.return_value.stdout = '{"streams":[{"codec_type":"video"},{"codec_type":"audio"}],"format":{"duration":"10"}}'
        self.assertEqual(inspect_video(Path('clip.mp4')), 10)

    @patch('subtitles.validation._run')
    def test_invalid_metadata_is_rejected(self, run):
        from subtitles.validation import inspect_video
        for text in ('broken', '{}', '{"streams":[{"codec_type":"audio"}],"format":{"duration":1}}', '{"streams":[{"codec_type":"video"},{"codec_type":"audio"}],"format":{"duration":"nan"}}'):
            run.return_value.stdout = text
            with self.assertRaises(ValidationError):
                inspect_video(Path('clip.mp4'))

    def test_timeout_terminates_subprocess(self):
        from subtitles.processing import ProcessingTimeout, _run
        with self.assertRaises(ProcessingTimeout):
            _run([sys.executable, '-c', 'import time; time.sleep(5)'], timeout=0.05)


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import close_old_connections
from django.test import TransactionTestCase


@override_settings(MAX_ACTIVE_JOBS_PER_USER=1)
class ConcurrentAdmissionTests(TransactionTestCase):
    def test_two_simultaneous_uploads_respect_quota(self):
        user = get_user_model().objects.create_user('parallel')
        barrier = Barrier(2)
        def inspect(path):
            barrier.wait(timeout=5)
            return 5
        def submit(index):
            close_old_connections()
            try:
                create_job(user, SimpleUploadedFile(f'clip{index}.mp4', b'video'))
                return 'accepted'
            except ValidationError:
                return 'refused'
            finally:
                close_old_connections()
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root), patch('subtitles.services.inspect_video', side_effect=inspect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(submit, [1, 2]))
            self.assertCountEqual(results, ['accepted', 'refused'])
            self.assertEqual(Job.objects.filter(owner=user).count(), 1)
            self.assertEqual(len(list(Path(root).rglob('*.mp4'))), 1)
