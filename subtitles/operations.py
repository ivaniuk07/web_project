"""Staff-only, read-only system overview. User file endpoints remain owner-only."""
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .models import Job
from .monitoring import capacity_snapshot


@login_required
@require_GET
def overview(request):
    if not request.user.is_active or not request.user.is_staff:
        raise PermissionDenied
    active = Job.objects.filter(status__in=[Job.Status.QUEUED, Job.Status.RUNNING])
    return render(request, 'subtitles/operations.html', {
        'snapshot': capacity_snapshot(),
        'active_total': active.count(),
        'active_jobs': active.select_related('owner').order_by('-created_at')[:100],
    })
