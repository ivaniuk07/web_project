import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from subtitles.models import Job, WorkerNode


class JobsApiTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        media = override_settings(MEDIA_ROOT=directory.name)
        media.enable()
        self.addCleanup(media.disable)
        self.owner = get_user_model().objects.create_user('api-owner')
        self.other = get_user_model().objects.create_user('api-other')
        self.api = APIClient(enforce_csrf_checks=True)
        self.api.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=self.owner).key}')

    def test_multipart_upload_queues_a_job_with_a_status_url(self):
        with patch('subtitles.services.inspect_video', return_value=4):
            response = self.api.post('/api/jobs/', {
                'video': SimpleUploadedFile('clip.mp4', b'video'),
                'subtitle_style': 'studio',
                'subtitle_language': 'pl',
            }, format='multipart')
        self.assertEqual(response.status_code, 201)
        job = Job.objects.get(owner=self.owner)
        self.assertIsNotNone(job.task_token)
        self.assertEqual(job.subtitle_style, 'studio')
        self.assertEqual(job.subtitle_language, 'pl')
        self.assertEqual(response.json()['subtitle_language'], 'pl')
        self.assertEqual(response.json()['status'], 'queued')
        self.assertEqual(response.json()['duration_seconds'], 4)
        self.assertEqual(job.duration_seconds, 4)
        self.assertTrue(response['Location'].endswith(f'/api/jobs/{job.pk}/'))

    def test_invalid_upload_does_not_create_a_job(self):
        response = self.api.post('/api/jobs/', {'video': SimpleUploadedFile('bad.txt', b'text')}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertIn('video', response.json())
        self.assertFalse(Job.objects.exists())

    def test_job_status_and_downloads_require_the_owner_token(self):
        now = timezone.now()
        job = Job.objects.create(
            owner=self.owner, original_name='clip.mp4', source='videos/input.mp4',
            status=Job.Status.SUCCEEDED, node_name='node-2',
            dispatched_at=now - timedelta(seconds=10), started_at=now - timedelta(seconds=7), finished_at=now,
            subtitles='subtitles/output.srt', result='rendered/output.mp4',
        )
        for field, content in ((job.subtitles, b'srt'), (job.result, b'mp4')):
            path = Path(field.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        detail = f'/api/jobs/{job.pk}/'
        response = self.api.get(detail)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['node'], 'node-2')
        self.assertEqual(data['dispatch_wait_seconds'], 3)
        self.assertEqual(data['processing_seconds'], 7)
        self.assertNotIn('task_token', data)
        for name, content in (('srt', b'srt'), ('mp4', b'mp4')):
            download = self.api.get(data['artifacts'][name])
            self.assertEqual(download.status_code, 200)
            self.assertEqual(b''.join(download.streaming_content), content)

        self.api.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=self.other).key}')
        for url in (detail, *data['artifacts'].values()):
            self.assertEqual(self.api.get(url).status_code, 404)
            self.assertEqual(APIClient().get(url).status_code, 401)

    def test_only_staff_can_read_worker_load_report(self):
        node = WorkerNode.objects.create(name='node-2', broker_url='redis://private@example.com/0')
        Job.objects.create(owner=self.owner, original_name='clip.mp4', source='videos/done.mp4',
                           status=Job.Status.SUCCEEDED, node=node, node_name=node.name)
        self.assertEqual(self.api.get('/api/operations/').status_code, 403)
        self.owner.is_staff = True
        self.owner.save(update_fields=['is_staff'])
        response = self.api.get('/api/operations/')
        self.assertEqual(response.status_code, 200)
        worker, = response.json()['nodes']
        self.assertEqual(worker['succeeded'], 1)
        self.assertNotIn('broker_url', worker)
