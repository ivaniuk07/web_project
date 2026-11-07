from pathlib import Path

from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import UploadForm
from .models import Job
from .services import create_job, process_job


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
    form = UploadForm(
        request.POST if request.method == 'POST' else None,
        request.FILES if request.method == 'POST' else None,
    )
    if request.method == 'POST' and form.is_valid():
        try:
            job = create_job(request.user, form.cleaned_data['video'])
        except ValidationError as exc:
            form.add_error('video', exc)
        else:
            return redirect('job_detail', job_id=job.pk)
    return render(request, 'subtitles/home.html', {
        'form': form,
        'jobs': Job.objects.filter(owner=request.user),
    })


def _owned_job(request, job_id):
    return get_object_or_404(Job, pk=job_id, owner=request.user)


@login_required
@require_GET
def job_detail(request, job_id):
    job = _owned_job(request, job_id)
    return render(request, 'subtitles/detail.html', {
        'job': job,
        'can_process': job.status in [Job.Status.QUEUED, Job.Status.FAILED],
    })


@login_required
@require_POST
def process_video(request, job_id):
    job = _owned_job(request, job_id)
    try:
        process_job(job)
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('job_detail', job_id=job_id)


@login_required
@require_GET
def download_subtitles(request, job_id):
    job = _owned_job(request, job_id)
    return _file_response(job.subtitles, True, f'{Path(job.original_name).stem}.srt', 'application/x-subrip')


@login_required
@require_GET
def video_result(request, job_id):
    job = _owned_job(request, job_id)
    if job.status != Job.Status.SUCCEEDED:
        raise Http404
    return _file_response(job.result, request.GET.get('download') == '1', f'{Path(job.original_name).stem}_subtitled.mp4', 'video/mp4')


def _file_response(field, attachment, filename, content_type):
    if not field:
        raise Http404
    try:
        file = field.open('rb')
    except (FileNotFoundError, ValueError) as exc:
        raise Http404 from exc
    return FileResponse(file, as_attachment=attachment, filename=filename, content_type=content_type)
