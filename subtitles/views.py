from pathlib import Path
import re

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .dispatcher import estimate_wait_time, get_system_status
from .models import Job, SubtitleLanguage, SubtitleStyle
from .processing import validate_video_upload
from .tasks import cancel_job as cancel_job_service, create_job, queue_job


class UploadForm(forms.Form):
    video = forms.FileField(
        label='Відеофайл',
    )
    subtitle_language = forms.ChoiceField(
        label='Мова субтитрів',
        choices=SubtitleLanguage.choices,
        required=False,
        help_text='Оберіть мову, якою говорять у відео.',
    )
    subtitle_style = forms.ChoiceField(
        label='Теми субтитрів',
        choices=SubtitleStyle.choices,
        initial=SubtitleStyle.CLASSIC,
        required=False,
        widget=forms.RadioSelect(attrs={'class': 'visually-hidden'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        maximum = settings.MAX_UPLOAD_BYTES / (1024 * 1024)
        minutes, seconds = divmod(settings.MAX_VIDEO_SECONDS, 60)
        duration = f'{minutes} хв' if not seconds else f'{settings.MAX_VIDEO_SECONDS} с'
        formats = ', '.join(settings.ALLOWED_VIDEO_EXTENSIONS)
        self.fields['video'].help_text = f'До {maximum:g} МіБ і {duration}. Формати: {formats}.'
        self.fields['video'].widget.attrs.update({
            'class': 'visually-hidden',
            'accept': ','.join(f'.{extension}' for extension in settings.ALLOWED_VIDEO_EXTENSIONS),
        })

    def clean_video(self):
        video = self.cleaned_data['video']
        validate_video_upload(video)
        return video

    def clean_subtitle_style(self):
        return self.cleaned_data['subtitle_style'] or SubtitleStyle.CLASSIC


@require_http_methods(['GET', 'POST'])
def register(request):
    if request.user.is_authenticated:
        return redirect('home')
    form = UserCreationForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        login(request, form.save())
        return redirect('home')
    return render(request, 'registration/register.html', {'form': form})


@login_required
@require_http_methods(['GET', 'POST'])
def home(request):
    form = UploadForm()
    if request.method == 'POST':
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                job = create_job(request.user, **form.cleaned_data)
            except ValidationError as exc:
                form.add_error('video', exc)
            else:
                return redirect('job_detail', job_id=job.pk)
    latest_result = Job.objects.filter(
        owner=request.user, status=Job.Status.SUCCEEDED,
    ).exclude(result='').first()
    return render(request, 'subtitles/home.html', {
        'form': form,
        'latest_result': latest_result,
    })


@login_required
@require_GET
def job_history(request):
    return render(request, 'subtitles/history.html', {
        'jobs': Job.objects.filter(owner=request.user),
    })


def get_user_job(request, job_id):
    return get_object_or_404(Job, pk=job_id, owner=request.user)


@login_required
@require_GET
def job_detail(request, job_id):
    job = get_user_job(request, job_id)
    return render(request, 'subtitles/detail.html', {
        'job': job,
        'waiting_estimate': estimate_wait_time(job),
        'can_process': job.status in Job.CAN_START,
        'can_cancel': job.status in Job.ACTIVE,
    })


@login_required
@require_POST
def process_video(request, job_id):
    job = get_user_job(request, job_id)
    try:
        queue_job(job)
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('job_detail', job_id=job_id)


@login_required
@require_POST
def cancel_job(request, job_id):
    cancel_job_service(get_user_job(request, job_id))
    return redirect('job_detail', job_id=job_id)


@login_required
@require_GET
def download_subtitles(request, job_id):
    return subtitles_response(get_user_job(request, job_id))


@login_required
@require_GET
def video_result(request, job_id):
    return video_response(
        get_user_job(request, job_id),
        attachment=request.GET.get('download') == '1',
        range_header=request.headers.get('Range') if 'If-Range' not in request.headers else None,
    )


@login_required
@require_GET
def system_status(request):
    if not request.user.is_active or not request.user.is_staff:
        raise PermissionDenied
    active = Job.objects.filter(status__in=Job.ACTIVE)
    return render(request, 'subtitles/operations.html', {
        'snapshot': get_system_status(),
        'active_total': active.count(),
        'active_jobs': active.select_related('owner').order_by('-created_at')[:100],
    })


def subtitles_response(job):
    if job.status == Job.Status.CANCELLED:
        raise Http404
    return _file_response(job.subtitles, True, f'{Path(job.original_name).stem}.srt', 'application/x-subrip')


def video_response(job, *, attachment, range_header=None):
    if job.status != Job.Status.SUCCEEDED:
        raise Http404
    response = _file_response(job.result, attachment, f'{Path(job.original_name).stem}_subtitled.mp4', 'video/mp4')
    response['Accept-Ranges'] = 'bytes'
    size = int(response['Content-Length'])
    byte_range = _parse_byte_range(range_header, size)
    if byte_range is None:
        return response

    start, end = byte_range
    if start > end:
        response.close()
        return HttpResponse(status=416, headers={
            'Accept-Ranges': 'bytes',
            'Content-Range': f'bytes */{size}',
            'Content-Length': '0',
        })

    file = response.file_to_stream
    file.seek(start)
    length = end - start + 1
    # Replacing the iterator disables WSGI's whole-file wrapper and keeps the file closer.
    response.streaming_content = _read_range(file, length, response.block_size)
    response.status_code = 206
    response['Content-Range'] = f'bytes {start}-{end}/{size}'
    response['Content-Length'] = str(length)
    return response


def _parse_byte_range(header, size):
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', header or '', flags=re.IGNORECASE)
    if match is None:
        return None
    first, last = match.groups()
    if not first and not last:
        return None

    last_byte = size - 1
    try:
        if not first:
            return max(size - int(last), 0), last_byte
        start = int(first)
        end = min(int(last), last_byte) if last else last_byte
        return start, end
    except ValueError:
        return None


def _read_range(file, remaining_bytes, block_size):
    while remaining_bytes > 0:
        bytes_to_read = min(block_size, remaining_bytes)
        chunk = file.read(bytes_to_read)
        if not chunk:
            break
        yield chunk
        remaining_bytes -= len(chunk)


def _file_response(field, attachment, filename, content_type):
    if not field:
        raise Http404
    try:
        file = field.open('rb')
    except (FileNotFoundError, ValueError) as exc:
        raise Http404 from exc
    return FileResponse(file, as_attachment=attachment, filename=filename, content_type=content_type)
