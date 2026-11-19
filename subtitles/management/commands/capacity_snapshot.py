"""Machine-readable telemetry for the operator's external scaling experiment."""
import json

from django.core.management.base import BaseCommand
from subtitles.balancing import get_system_status


class Command(BaseCommand):
    help = 'Print read-only node capacity without broker URLs or credentials.'

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(get_system_status(), ensure_ascii=False))
