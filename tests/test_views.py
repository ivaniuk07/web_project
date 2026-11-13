import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from subtitles.models import Job, WorkerNode
from subtitles.processing import ProcessingError


class JobViewTests(TestCase):
    def setUp(self):
        from django.utils import timezone
        WorkerNode.objects.create(name='node-1', broker_url='redis://localhost:6379/0', queue='subtitles-local', heartbeat_at=timezone.now())
        publish_patch = patch('subtitles.tasks.process_video_task.apply_async')
        publish_patch.start()
        self.addCleanup(publish_patch.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.media = override_settings(MEDIA_ROOT=self.directory.name)
        self.media.enable()
        self.addCleanup(self.media.disable)
        inspect_patch = patch('subtitles.services.inspect_video', return_value=5)
        inspect_patch.start()
        self.addCleanup(inspect_patch.stop)
        self.user = get_user_model().objects.create_user('alice', password='test-password-142')
        self.other = get_user_model().objects.create_user('bob', password='test-password-242')
        self.client.force_login(self.user)
        self.job = Job.objects.create(owner=self.user, original_name='lesson.mp4', source='videos/lesson.mp4')
        source = Path(self.job.source.path)
        source.parent.mkdir(parents=True)
        source.write_bytes(b'video')

    def submit_and_run(self, follow=False):
        from subtitles.services import process_job
        response = self.client.post(reverse('process_video', args=[self.job.pk]))
        if response.status_code == 302:
            self.job.refresh_from_db()
            if self.job.status == Job.Status.QUEUED and self.job.task_token:
                process_job(self.job, token=self.job.task_token)
            if follow:
                return self.client.get(response.url)
        return response

    def test_anonymous_redirects_to_login(self):
        self.client.logout()
        for url in ('/', '/history/', reverse('job_detail', args=[self.job.pk]), reverse('process_video', args=[self.job.pk]), reverse('video_result', args=[self.job.pk]), reverse('download_subtitles', args=[self.job.pk])):
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_video_supports_byte_ranges_for_seeking(self):
        self.job.status = Job.Status.SUCCEEDED
        self.job.result = 'rendered/lesson.mp4'
        self.job.save(update_fields=['status', 'result'])
        result = Path(self.job.result.path)
        result.parent.mkdir()
        result.write_bytes(b'0123456789')
        url = reverse('video_result', args=[self.job.pk])

        for requested, expected_range, content in (
            ('bytes=2-4', 'bytes 2-4/10', b'234'),
            ('bytes=7-', 'bytes 7-9/10', b'789'),
            ('bytes=-2', 'bytes 8-9/10', b'89'),
        ):
            with self.subTest(requested=requested):
                response = self.client.get(url, HTTP_RANGE=requested)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response['Accept-Ranges'], 'bytes')
                self.assertEqual(response['Content-Range'], expected_range)
                self.assertEqual(response['Content-Length'], str(len(content)))
                self.assertEqual(b''.join(response.streaming_content), content)

        response = self.client.get(url, HTTP_RANGE='bytes=10-')
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response['Content-Range'], 'bytes */10')

    def test_registration_logs_in(self):
        self.client.logout()
        response = self.client.post(reverse('register'), {'username': 'charlie', 'password1': 'Unique-password-142!', 'password2': 'Unique-password-142!'})
        self.assertRedirects(response, '/')
        self.assertEqual(self.client.get('/').wsgi_request.user.username, 'charlie')

    def test_login_and_post_logout(self):
        self.client.logout()
        self.assertRedirects(self.client.post(reverse('login'), {'username': 'alice', 'password': 'test-password-142'}), '/')
        self.assertEqual(self.client.get(reverse('logout')).status_code, 405)
        self.assertRedirects(self.client.post(reverse('logout')), reverse('login'))

    def test_history_only_shows_own_jobs(self):
        Job.objects.filter(pk=self.job.pk).update(status=Job.Status.SUCCEEDED, result='rendered/lesson.mp4')
        waiting_job = Job.objects.create(
            owner=self.user, original_name='waiting.mp4', source='videos/waiting.mp4',
        )
        foreign_job = Job.objects.create(
            owner=self.other,
            original_name='private-bob.mp4',
            source='videos/bob.mp4',
            status=Job.Status.SUCCEEDED,
            result='rendered/bob.mp4',
        )
        response = self.client.get('/')
        self.assertContains(response, 'lesson.mp4')
        self.assertNotContains(response, 'private-bob.mp4')
        self.assertContains(response, reverse('video_result', args=[self.job.pk]))
        self.assertNotContains(response, reverse('video_result', args=[foreign_job.pk]))
        self.assertNotContains(response, waiting_job.original_name)

        history = self.client.get('/history/')
        self.assertContains(history, 'lesson.mp4')
        self.assertContains(history, waiting_job.original_name)
        self.assertNotContains(history, foreign_job.original_name)

    def test_foreign_job_is_inaccessible_for_all_actions(self):
        self.client.force_login(self.other)
        for name in ('job_detail', 'video_result', 'download_subtitles'):
            self.assertEqual(self.client.get(reverse(name, args=[self.job.pk])).status_code, 404)
        self.assertEqual(self.submit_and_run().status_code, 404)
        self.assertEqual(self.client.get(reverse('video_result', args=[self.job.pk]) + '?download=1').status_code, 404)

    def test_upload_persists_owner_and_file(self):
        response = self.client.post('/', {'video': SimpleUploadedFile('new.mp4', b'new-video')})
        job = Job.objects.get(original_name='new.mp4')
        self.assertRedirects(response, reverse('job_detail', args=[job.pk]))
        self.assertEqual(job.owner, self.user)
        self.assertEqual(Path(job.source.path).read_bytes(), b'new-video')
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertEqual(job.subtitle_style, 'classic')
        self.assertEqual(job.subtitle_language, '')

    def test_upload_saves_selected_subtitle_style(self):
        response = self.client.post('/', {
            'video': SimpleUploadedFile('accent.mp4', b'new-video'),
            'subtitle_style': 'yellow',
            'subtitle_language': 'uk',
        })
        job = Job.objects.get(original_name='accent.mp4')
        self.assertRedirects(response, reverse('job_detail', args=[job.pk]))
        self.assertEqual(job.subtitle_style, 'yellow')
        self.assertEqual(job.subtitle_language, 'uk')
        self.assertContains(self.client.get(response.url), 'Стиль субтитрів — Жовтий акцент')

    def test_upload_page_offers_the_new_subtitle_themes(self):
        response = self.client.get('/')
        for theme in ('cinema', 'neon', 'studio'):
            with self.subTest(theme=theme):
                self.assertContains(response, f'name="subtitle_style" value="{theme}"')

    def test_upload_saves_a_new_theme(self):
        response = self.client.post('/', {
            'video': SimpleUploadedFile('theme.mp4', b'new-video'),
            'subtitle_style': 'studio',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Job.objects.get(original_name='theme.mp4').subtitle_style, 'studio')

    def test_upload_rejects_unsupported_subtitle_style(self):
        response = self.client.post('/', {
            'video': SimpleUploadedFile('invalid.mp4', b'new-video'),
            'subtitle_style': 'Fontsize=100',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('subtitle_style', response.context['form'].errors)
        self.assertFalse(Job.objects.filter(original_name='invalid.mp4').exists())

    def test_empty_and_invalid_upload(self):
        for data in ({}, {'video': SimpleUploadedFile('bad.txt', b'text')}):
            self.assertIn('video', self.client.post('/', data).context['form'].errors)

    def test_processing_success_records_results_and_prevents_duplicate_work(self):
        self.job.subtitle_language = 'uk'
        self.job.save(update_fields=['subtitle_language'])
        def generate(source, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n', encoding='utf-8')
        def render(source, captions, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'mp4-result')
        with patch('subtitles.services.generate_subtitles', side_effect=generate) as transcribe, patch('subtitles.services.burn_subtitles', side_effect=render):
            response = self.submit_and_run(follow=True)
            self.submit_and_run()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.SUCCEEDED)
        self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(transcribe.call_args.kwargs['language'], 'uk')
        self.assertContains(response, '<video')
        self.assertContains(response, 'Завантажити SRT')
        self.assertEqual(b''.join(self.client.get(reverse('video_result', args=[self.job.pk])).streaming_content), b'mp4-result')
        download = self.client.get(reverse('video_result', args=[self.job.pk]) + '?download=1')
        self.assertTrue(download['Content-Disposition'].startswith('attachment'))
        self.assertEqual(b''.join(download.streaming_content), b'mp4-result')
        self.assertIn(b'Hello', b''.join(self.client.get(reverse('download_subtitles', args=[self.job.pk])).streaming_content))

    def test_processing_failure_is_persisted(self):
        with patch('subtitles.services.generate_subtitles', side_effect=ProcessingError('Файл пошкоджений.')):
            response = self.submit_and_run(follow=True)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.FAILED)
        self.assertEqual(self.job.error, 'Файл пошкоджений.')
        self.assertContains(response, 'Файл пошкоджений.')

    def test_running_job_does_not_start_twice(self):
        Job.objects.filter(pk=self.job.pk).update(status=Job.Status.RUNNING)
        with patch('subtitles.services.generate_subtitles') as generate:
            self.submit_and_run()
        generate.assert_not_called()

    def test_unowned_legacy_files_are_not_exposed(self):
        (Path(self.directory.name) / 'videos' / 'unowned.mp4').write_bytes(b'private')
        self.assertNotContains(self.client.get('/'), 'unowned.mp4')
        self.assertEqual(self.client.get('/video/unowned.mp4/').status_code, 404)

    def test_csrf_is_required_for_upload_and_processing(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        self.assertEqual(browser.post('/').status_code, 403)
        self.assertEqual(browser.post(reverse('process_video', args=[self.job.pk])).status_code, 403)

    def test_explicit_legacy_import_is_idempotent_and_keeps_owner(self):
        from django.core.management import call_command
        legacy = Path(self.directory.name) / 'videos' / 'old.mp4'
        legacy.write_bytes(b'old-video')
        captions = Path(self.directory.name) / 'subtitles'
        captions.mkdir()
        (captions / 'old.mp4.srt').write_text('captions', encoding='utf-8')
        call_command('import_legacy_videos', owner='alice', verbosity=0)
        call_command('import_legacy_videos', owner='bob', verbosity=0)
        job = Job.objects.get(source='videos/old.mp4')
        self.assertEqual(job.owner, self.user)
        self.assertEqual(job.subtitles.name, 'subtitles/old.mp4.srt')
        self.assertEqual(Job.objects.filter(source='videos/old.mp4').count(), 1)

    def test_missing_result_file_returns_404(self):
        Job.objects.filter(pk=self.job.pk).update(status=Job.Status.SUCCEEDED, result='rendered/gone.mp4', subtitles='subtitles/gone.srt')
        self.assertEqual(self.client.get(reverse('video_result', args=[self.job.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('download_subtitles', args=[self.job.pk])).status_code, 404)

    def test_interrupted_job_can_be_recovered(self):
        from django.core.management import call_command
        Job.objects.filter(pk=self.job.pk).update(status=Job.Status.RUNNING)
        call_command('recover_job', str(self.job.pk), verbosity=0)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.FAILED)
        self.assertEqual(self.job.owner, self.user)
        self.assertContains(self.client.get(reverse('job_detail', args=[self.job.pk])), 'Створити відео із субтитрами')

    def test_render_failure_keeps_srt_for_retry(self):
        response = self.client.post('/', {
            'video': SimpleUploadedFile('retry.mp4', b'new-video'),
            'subtitle_style': 'dark-box',
        })
        self.job = Job.objects.get(original_name='retry.mp4')
        self.assertRedirects(response, reverse('job_detail', args=[self.job.pk]))

        def generate(source, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('captions', encoding='utf-8')
        def render(source, captions, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'mp4')
        with patch('subtitles.services.generate_subtitles', side_effect=generate) as generate_mock, patch('subtitles.services.burn_subtitles', side_effect=ProcessingError('Кодування перервано.')), self.assertLogs('subtitles.services', level='ERROR'):
            self.submit_and_run()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.FAILED)
        self.assertTrue(Path(self.job.subtitles.path).is_file())
        with patch('subtitles.services.generate_subtitles') as second_generate, patch('subtitles.services.burn_subtitles', side_effect=render) as second_render:
            self.submit_and_run()
        second_generate.assert_not_called()
        self.assertEqual(second_render.call_args.kwargs.get('subtitle_style'), 'dark-box')
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, Job.Status.SUCCEEDED)
