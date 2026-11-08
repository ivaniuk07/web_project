import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings


class DeploymentCheckTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.root.name)
        self.override.enable()
        self.addCleanup(self.override.disable)

    @patch('subtitles.management.commands.check_deployment.Connection')
    def test_shared_marker_can_be_written_and_read(self, connection):
        call_command('check_deployment', write_marker='host-a', stdout=io.StringIO())
        marker = Path(self.root.name) / '.deployment-checks' / 'host-a.json'
        data = json.loads(marker.read_text())
        self.assertTrue(data['nonce'])
        result = io.StringIO()
        call_command('check_deployment', read_marker='host-a', stdout=result)
        self.assertIn(data['nonce'], result.getvalue())

    @patch('subtitles.management.commands.check_deployment.Connection')
    def test_missing_marker_fails(self, connection):
        with self.assertRaises(CommandError):
            call_command('check_deployment', read_marker='absent', stdout=io.StringIO())

    @patch('subtitles.management.commands.check_deployment.Connection')
    def test_marker_path_traversal_is_rejected(self, connection):
        with self.assertRaises(CommandError):
            call_command('check_deployment', write_marker='../outside', stdout=io.StringIO())

    @patch('subtitles.management.commands.check_deployment.Connection')
    def test_missing_shared_directory_is_not_created(self, connection):
        missing = Path(self.root.name) / 'not-mounted'
        with override_settings(MEDIA_ROOT=missing), self.assertRaises(CommandError):
            call_command('check_deployment', stdout=io.StringIO())
        self.assertFalse(missing.exists())
