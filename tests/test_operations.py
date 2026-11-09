import io
import json
import uuid

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from subtitles.models import Job, WorkerNode


class OperationsTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user('operator', is_staff=True)
        self.user = get_user_model().objects.create_user('regular')
        self.node = WorkerNode.objects.create(name='node-1', broker_url='redis://:PRIVATE_BROKER_PASSWORD@redis:6379/0', heartbeat_at=timezone.now())
        self.job = Job.objects.create(owner=self.user, source='videos/operations.mp4', original_name='operations.mp4', node=self.node, status='running', task_token=uuid.uuid4())
        Job.objects.create(owner=self.user, source='videos/not-submitted.mp4', original_name='not submitted')
        Job.objects.create(owner=self.user, source='videos/waiting.mp4', original_name='waiting', task_token=uuid.uuid4())

    def test_anonymous_redirects_and_nonstaff_is_forbidden(self):
        self.assertEqual(self.client.get('/operations/').status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/operations/').status_code, 403)

    def test_staff_sees_load_without_broker_secret_or_task_token(self):
        self.client.force_login(self.staff)
        response = self.client.get('/operations/')
        self.assertContains(response, 'node-1')
        self.assertContains(response, 'operations.mp4')
        self.assertNotContains(response, 'PRIVATE_BROKER_PASSWORD')
        self.assertNotContains(response, str(self.job.task_token))
        self.assertNotContains(response, 'redis://')
        self.assertEqual(response.context['snapshot']['pending'], 1)
        self.assertEqual(response.context['snapshot']['awaiting_submission'], 1)
        self.assertEqual(response.context['snapshot']['nodes'][0]['running'], 1)
        self.assertEqual(self.client.post('/operations/').status_code, 405)

    def test_snapshot_command_has_no_credentials_and_distinguishes_work(self):
        output = io.StringIO()
        call_command('capacity_snapshot', stdout=output)
        data = json.loads(output.getvalue())
        self.assertEqual(data['waiting'], 1)
        self.assertEqual(data['running'], 1)
        self.assertNotIn('PRIVATE_BROKER_PASSWORD', output.getvalue())
        self.assertNotIn('broker_url', output.getvalue())

    def test_staff_permission_does_not_bypass_owner_downloads(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(f'/jobs/{self.job.pk}/').status_code, 404)
