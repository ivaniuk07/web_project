from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.utils import timezone
from subtitles.models import Job


class Command(BaseCommand):
    help = 'After stopping processing servers, make an interrupted running job retryable.'

    def add_arguments(self, parser):
        parser.add_argument('job_id')

    def handle(self, *args, **options):
        try:
            changed = Job.objects.filter(pk=options['job_id'], status=Job.Status.RUNNING).update(
                status=Job.Status.FAILED,
                error='Попередню обробку перервано. Запустіть завдання повторно.',
                updated_at=timezone.now(),
            )
        except ValidationError as exc:
            raise CommandError('Некоректний ідентифікатор задачі.') from exc
        if not changed:
            raise CommandError('Задачу в стані обробки не знайдено.')
        self.stdout.write(self.style.SUCCESS('Задача доступна для повторного запуску.'))
