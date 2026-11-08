import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from subtitles.scheduling import Dispatcher, sync_nodes


class Command(BaseCommand):
    help = 'Dispatch pending jobs and retry unavailable compute nodes.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        sync_nodes()
        dispatcher = Dispatcher()
        try:
            while True:
                close_old_connections()
                dispatcher.recover()
                for _ in range(100):
                    if not dispatcher.dispatch_once():
                        break
                if options['once']:
                    return
                time.sleep(settings.DISPATCH_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            self.stdout.write('Dispatcher stopped.')
