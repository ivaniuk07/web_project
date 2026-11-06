import tempfile
from pathlib import Path
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings


class HomeTests(TestCase):
    def test_home_shows_upload_form(self):
        response = self.client.get('/')
        self.assertContains(response, 'Завантажити відео')

    def test_upload_saves_file_and_lists_it(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root):
            video = SimpleUploadedFile('lesson.mp4', b'demo-video', content_type='video/mp4')
            response = self.client.post('/', {'video': video})
            self.assertRedirects(response, '/')
            files = list((Path(root) / 'videos').iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b'demo-video')
            self.assertContains(self.client.get('/'), 'lesson')

    def test_invalid_extension_is_rejected(self):
        video = SimpleUploadedFile('file.txt', b'text')
        response = self.client.post('/', {'video': video})
        self.assertEqual(response.status_code, 200)
        self.assertIn('video', response.context['form'].errors)

    def test_empty_upload_shows_error(self):
        response = self.client.post('/')
        self.assertIn('video', response.context['form'].errors)

    def test_process_missing_file_returns_404(self):
        self.assertEqual(self.client.post('/process/missing.mp4/').status_code, 404)

    def test_download_srt(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root):
            output = Path(root) / 'subtitles'
            output.mkdir()
            (output / 'video.mp4.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n', encoding='utf-8')
            response = self.client.get('/download/video.mp4/')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Hello', b''.join(response.streaming_content))

    def test_process_error_is_shown_on_page(self):
        from unittest.mock import patch
        from subtitles.processing import ProcessingError
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root):
            videos = Path(root) / 'videos'
            videos.mkdir()
            (videos / 'broken.mp4').write_bytes(b'broken')
            with patch('subtitles.processing.generate_subtitles', side_effect=ProcessingError('Файл пошкоджений.')):
                response = self.client.post('/process/broken.mp4/', follow=True)
            self.assertContains(response, 'Файл пошкоджений.')

    def test_process_creates_download_link(self):
        from unittest.mock import patch
        def generate(source, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n', encoding='utf-8')
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root):
            videos = Path(root) / 'videos'
            videos.mkdir()
            (videos / 'lesson.mp4').write_bytes(b'video')
            with patch('subtitles.processing.generate_subtitles', side_effect=generate), patch('subtitles.processing.burn_subtitles'):
                response = self.client.post('/process/lesson.mp4/', follow=True)
            self.assertContains(response, 'Завантажити SRT')
            self.assertContains(response, '/download/lesson.mp4/')

    def test_video_preview_and_download(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root):
            output = Path(root) / 'rendered'
            output.mkdir()
            (output / 'lesson.mp4.mp4').write_bytes(b'processed-video')
            response = self.client.get('/video/lesson.mp4/')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'video/mp4')
            self.assertTrue(response['Content-Disposition'].startswith('inline'))
            self.assertEqual(b''.join(response.streaming_content), b'processed-video')
            download = self.client.get('/video/lesson.mp4/?download=1')
            self.assertTrue(download['Content-Disposition'].startswith('attachment'))
            download.close()

    def test_existing_srt_can_be_rendered_without_transcribing_again(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=root):
            videos, captions = Path(root) / 'videos', Path(root) / 'subtitles'
            videos.mkdir()
            captions.mkdir()
            (videos / 'lesson.mp4').write_bytes(b'video')
            (captions / 'lesson.mp4.srt').write_text('captions', encoding='utf-8')
            def render(source, srt, destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b'processed-video')
            with patch('subtitles.processing.generate_subtitles') as transcribe, patch('subtitles.processing.burn_subtitles', side_effect=render):
                response = self.client.post('/process/lesson.mp4/', follow=True)
            transcribe.assert_not_called()
            self.assertContains(response, '<video')
            self.assertContains(response, 'Завантажити відео')
            self.assertContains(response, 'Завантажити SRT')
