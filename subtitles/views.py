from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .downloads import subtitles_response, video_response
from .forms import UploadForm
from .models import Job
from .monitoring import estimate_wait
from .services import create_job, enqueue_job, cancel_job as cancel_job_service


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
            job = create_job(
                request.user, form.cleaned_data['video'],
                subtitle_style=form.cleaned_data['subtitle_style'],
                subtitle_language=form.cleaned_data['subtitle_language'],
            )
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


def _owned_job(request, job_id):
    return get_object_or_404(Job, pk=job_id, owner=request.user)


@login_required
@require_GET
def job_detail(request, job_id):
    job = _owned_job(request, job_id)
    return render(request, 'subtitles/detail.html', {
        'job': job,
        'waiting_estimate': estimate_wait(job),
        'can_process': job.status == Job.Status.FAILED or (job.status == Job.Status.QUEUED and not job.task_token),
        'can_cancel': job.status in [Job.Status.QUEUED, Job.Status.RUNNING],
    })


@login_required
@require_POST
def process_video(request, job_id):
    job = _owned_job(request, job_id)
    try:
        enqueue_job(job)
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('job_detail', job_id=job_id)


@login_required
@require_POST
def cancel_job(request, job_id):
    cancel_job_service(_owned_job(request, job_id))
    return redirect('job_detail', job_id=job_id)


@login_required
@require_GET
def download_subtitles(request, job_id):
    return subtitles_response(_owned_job(request, job_id))


@login_required
@require_GET
def video_result(request, job_id):
    return video_response(
        _owned_job(request, job_id),
        attachment=request.GET.get('download') == '1',
        range_header=request.headers.get('Range') if 'If-Range' not in request.headers else None,
    )
