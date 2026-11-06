from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from subtitles.processing import ProcessingError, generate_subtitles


class Command(BaseCommand):
    help = 'Generate SRT subtitles for one local video.'

    def add_arguments(self, parser):
        parser.add_argument('video', type=Path)
        parser.add_argument('--output', type=Path)

    def handle(self, *args, **options):
        source = options['video']
        if not source.is_file():
            raise CommandError('Відеофайл не знайдено.')
        destination = options['output'] or source.with_suffix('.srt')
        if source.resolve() == destination.resolve():
            raise CommandError('Файл результату має відрізнятися від відеофайлу.')
        try:
            generate_subtitles(source, destination)
        except ProcessingError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f'Субтитри збережено: {destination}'))
