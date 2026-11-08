"""Check a host's real database, broker and shared filesystem access."""
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from kombu import Connection

from subtitles.models import Job


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class Command(BaseCommand):
    help = 'Verify PostgreSQL, Redis and shared media on a deployment host.'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--write-marker')
        group.add_argument('--read-marker')
        parser.add_argument('--all-brokers', action='store_true')
        parser.add_argument('--job')

    def handle(self, *args, **options):
        label = options['write_marker'] or options['read_marker']
        if label and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', label):
            raise CommandError('Ім’я маркера має містити літери, цифри, дефіс або підкреслення.')
        root = Path(settings.MEDIA_ROOT)
        if not root.is_dir():
            raise CommandError('Папка медіа відсутня. Перевірте монтування спільного сховища.')
        report = {'node': settings.WORKER_NODE_NAME}
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_database()')
                report['database'] = cursor.fetchone()[0]
        except Exception as exc:
            raise CommandError('Немає підключення до PostgreSQL.') from exc
        brokers = [('own', settings.CELERY_BROKER_URL)]
        if options['all_brokers']:
            brokers.extend((node['name'], node['broker_url']) for node in settings.WORKER_NODES)
        for name, url in brokers:
            try:
                with Connection(url, connect_timeout=3, transport_options={'socket_connect_timeout': 3, 'socket_timeout': 3}) as broker:
                    broker.ensure_connection(max_retries=0)
            except Exception as exc:
                raise CommandError(f'Немає підключення до Redis вузла {name}.') from exc
        report['brokers'] = [name for name, _ in brokers]
        probe = None
        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix='.deployment-probe-', delete=False) as file:
                probe = Path(file.name)
                file.write(b'shared-media-check')
            if probe.read_bytes() != b'shared-media-check':
                raise OSError('Read/write mismatch')
        except OSError as exc:
            raise CommandError('Немає доступу на читання й запис до спільної папки медіа.') from exc
        finally:
            if probe:
                probe.unlink(missing_ok=True)
        report['media_writable'] = True
        if label:
            marker = root / '.deployment-checks' / f'{label}.json'
            try:
                if options['write_marker']:
                    marker.parent.mkdir(exist_ok=True)
                    data = {'label': label, 'nonce': uuid.uuid4().hex, 'database': report['database']}
                    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=marker.parent, delete=False) as file:
                        temporary = Path(file.name)
                        try:
                            json.dump(data, file)
                            file.close()
                            os.replace(temporary, marker)
                        finally:
                            temporary.unlink(missing_ok=True)
                data = json.loads(marker.read_text(encoding='utf-8'))
                if data['database'] != report['database'] or not data['nonce']:
                    raise ValueError
                report['marker'] = data
                report['marker_sha256'] = file_hash(marker)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise CommandError('Спільний маркер недоступний або некоректний. Перевірте монтування тієї самої папки на обох хостах.') from exc
        if options['job']:
            try:
                job = Job.objects.get(pk=options['job'])
                report['job'] = {'id': str(job.pk), 'node': job.node_name, 'status': job.status}
                report['files'] = {
                    name: file_hash(Path(field.path))
                    for name, field in [('source', job.source), ('subtitles', job.subtitles), ('video', job.result)]
                    if field
                }
            except Exception as exc:
                raise CommandError('Не вдалося прочитати задачу або її файли зі спільного сховища.') from exc
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
