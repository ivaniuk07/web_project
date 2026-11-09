"""Machine-readable telemetry for the operator's external scaling experiment."""
import json

from django.core.management.base import BaseCommand
from subtitles.monitoring import capacity_snapshot


class Command(BaseCommand):
    help = 'Print read-only node capacity without broker URLs or credentials.'

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(capacity_snapshot(), ensure_ascii=False))
