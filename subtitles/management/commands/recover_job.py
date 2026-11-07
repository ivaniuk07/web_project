from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db.models import Q
from subtitles.models import Job


class Command(BaseCommand):
    help = 'After stopping workers, make an interrupted running or enqueued job retryable.'

    def add_arguments(self, parser):
        parser.add_argument('job_id')

    def handle(self, *args, **options):
        try:
            recoverable = Q(status=Job.Status.RUNNING) | Q(status=Job.Status.QUEUED, task_token__isnull=False)
            changed = Job.objects.filter(recoverable, pk=options['job_id']).update(
                status=Job.Status.FAILED,
                task_token=None, stage='Обробку перервано',
                error='Попередню обробку перервано. Запустіть завдання повторно.',
                updated_at=timezone.now(),
            )
        except ValidationError as exc:
            raise CommandError('Некоректний ідентифікатор задачі.') from exc
        if not changed:
            raise CommandError('Задачу в стані обробки не знайдено.')
        self.stdout.write(self.style.SUCCESS('Задача доступна для повторного запуску.'))
