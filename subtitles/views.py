from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.shortcuts import redirect, render
from django.utils.text import get_valid_filename
from django.views.decorators.http import require_http_methods

from .forms import UploadForm


@require_http_methods(['GET', 'POST'])
def home(request):
    storage = FileSystemStorage(location=Path(settings.MEDIA_ROOT) / 'videos')
    form = UploadForm(
        request.POST if request.method == 'POST' else None,
        request.FILES if request.method == 'POST' else None,
    )
    if request.method == 'POST' and form.is_valid():
        video = form.cleaned_data['video']
        filename = get_valid_filename(video.name)
        storage.save(f'{uuid4().hex}_{filename}', video)
        return redirect('home')

    files = []
    if storage.exists(''):
        _, files = storage.listdir('')
    results = Path(settings.MEDIA_ROOT) / 'subtitles'
    rendered = Path(settings.MEDIA_ROOT) / 'rendered'
    videos = [
        {
            'name': name,
            'ready': (results / f'{name}.srt').is_file(),
            'video_ready': (rendered / f'{name}.mp4').is_file(),
        }
        for name in sorted(files)
    ]
    return render(request, 'subtitles/home.html', {'form': form, 'files': videos})


def _video_path(filename):
    from django.http import Http404
    if Path(filename).name != filename or filename.startswith('.'):
        raise Http404
    return Path(settings.MEDIA_ROOT) / 'videos' / filename


@require_http_methods(['POST'])
def process_video(request, filename):
    from django.http import Http404
    from django.contrib import messages
    from .processing import ProcessingError, generate_subtitles, burn_subtitles

    source = _video_path(filename)
    if not source.is_file():
        raise Http404
    destination = Path(settings.MEDIA_ROOT) / 'subtitles' / f'{filename}.srt'
    video_result = Path(settings.MEDIA_ROOT) / 'rendered' / f'{filename}.mp4'
    try:
        if not destination.is_file():
            generate_subtitles(source, destination)
        burn_subtitles(source, destination, video_result)
    except ProcessingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Відео із субтитрами готове. Можна завантажити MP4 та SRT.')
    return redirect('home')


@require_http_methods(['GET'])
def download_subtitles(request, filename):
    from django.http import FileResponse, Http404

    _video_path(filename)
    output = Path(settings.MEDIA_ROOT) / 'subtitles' / f'{filename}.srt'
    try:
        file = output.open('rb')
    except FileNotFoundError as exc:
        raise Http404 from exc
    return FileResponse(file, as_attachment=True, filename=f'{Path(filename).stem}.srt', content_type='application/x-subrip')


@require_http_methods(['GET'])
def video_result(request, filename):
    from django.http import FileResponse, Http404

    _video_path(filename)
    output = Path(settings.MEDIA_ROOT) / 'rendered' / f'{filename}.mp4'
    try:
        file = output.open('rb')
    except FileNotFoundError as exc:
        raise Http404 from exc
    return FileResponse(
        file,
        as_attachment=request.GET.get('download') == '1',
        filename=f'{Path(filename).stem}_subtitled.mp4',
        content_type='video/mp4',
    )
