from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from subtitles.models import Job


class Command(BaseCommand):
    help = 'Assign unowned version 0.1 files to an explicitly selected user.'

    def add_arguments(self, parser):
        parser.add_argument('--owner', required=True, help='Username of the owner of legacy files.')

    def handle(self, *args, **options):
        try:
            owner = get_user_model().objects.get(username=options['owner'])
        except get_user_model().DoesNotExist as exc:
            raise CommandError('Користувача не знайдено. Спочатку зареєструйте обліковий запис.') from exc
        root = Path(settings.MEDIA_ROOT)
        videos = root / 'videos'
        if not videos.is_dir():
            self.stdout.write('Імпортовано 0 відео.')
            return
        created = 0
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=owner.pk)
            for source in sorted(videos.iterdir()):
                if not source.is_file() or source.name.startswith('.'):
                    continue
                source_name = f'videos/{source.name}'
                if Job.objects.filter(source=source_name).exists():
                    continue
                captions = f'subtitles/{source.name}.srt'
                result = f'rendered/{source.name}.mp4'
                ready = (root / result).is_file()
                Job.objects.create(
                    owner=owner, original_name=source.name, source=source_name,
                    subtitles=captions if (root / captions).is_file() else '',
                    result=result if ready else '',
                    status=Job.Status.SUCCEEDED if ready else Job.Status.QUEUED,
                )
                created += 1
        self.stdout.write(self.style.SUCCESS(f'Імпортовано {created} відео.'))
