import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from subtitles.dispatcher import dispatch_next_job, requeue_stalled_jobs, sync_worker_nodes


class Command(BaseCommand):
    help = 'Dispatch pending jobs and retry unavailable compute nodes.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        sync_worker_nodes()
        try:
            while True:
                close_old_connections()
                requeue_stalled_jobs()
                for _ in range(100):
                    if not dispatch_next_job():
                        break
                if options['once']:
                    return
                time.sleep(settings.DISPATCH_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            self.stdout.write('Dispatcher stopped.')
