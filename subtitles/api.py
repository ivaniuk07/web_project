from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import generics, serializers, status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView

from .downloads import subtitles_response, video_response
from .languages import SubtitleLanguage
from .models import Job
from .monitoring import capacity_snapshot, estimate_wait
from .services import create_job
from .subtitle_styles import SubtitleStyle


class JobUploadSerializer(serializers.Serializer):
    video = serializers.FileField()
    subtitle_style = serializers.ChoiceField(choices=SubtitleStyle.choices, default=SubtitleStyle.CLASSIC)
    subtitle_language = serializers.ChoiceField(choices=SubtitleLanguage.choices, default=SubtitleLanguage.AUTO)


class JobSerializer(serializers.ModelSerializer):
    node = serializers.CharField(source='node_name', read_only=True)
    estimated_wait_seconds = serializers.SerializerMethodField()
    dispatch_wait_seconds = serializers.SerializerMethodField()
    processing_seconds = serializers.SerializerMethodField()
    artifacts = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = (
            'id', 'original_name', 'duration_seconds', 'subtitle_style', 'subtitle_language', 'status', 'stage', 'node', 'error',
            'created_at', 'dispatched_at', 'started_at', 'finished_at',
            'estimated_wait_seconds', 'dispatch_wait_seconds', 'processing_seconds', 'artifacts',
        )

    def get_estimated_wait_seconds(self, job):
        estimate = estimate_wait(job)
        return estimate['seconds'] if estimate else None

    def get_dispatch_wait_seconds(self, job):
        if job.dispatched_at and job.started_at:
            return max(0, (job.started_at - job.dispatched_at).total_seconds())

    def get_processing_seconds(self, job):
        if job.started_at:
            end = job.finished_at or timezone.now()
            return max(0, (end - job.started_at).total_seconds())

    def get_artifacts(self, job):
        request = self.context['request']
        args = (job.pk,)
        srt = job.subtitles and job.status != Job.Status.CANCELLED
        mp4 = job.result and job.status == Job.Status.SUCCEEDED
        return {
            'srt': reverse('jobs-api:subtitles', args=args, request=request) if srt else None,
            'mp4': reverse('jobs-api:video', args=args, request=request) if mp4 else None,
        }


class JobCreateView(generics.GenericAPIView):
    serializer_class = JobUploadSerializer
    parser_classes = [MultiPartParser]

    def post(self, request):
        upload = self.get_serializer(data=request.data)
        upload.is_valid(raise_exception=True)
        try:
            job = create_job(request.user, enqueue=True, **upload.validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'video': exc.messages}) from exc
        location = reverse('jobs-api:detail', args=(job.pk,), request=request)
        body = JobSerializer(job, context=self.get_serializer_context()).data
        return Response(body, status=status.HTTP_201_CREATED, headers={'Location': location})


class JobDetailView(generics.RetrieveAPIView):
    serializer_class = JobSerializer

    def get_queryset(self):
        return Job.objects.filter(owner=self.request.user)


class JobSubtitlesView(JobDetailView):
    def get(self, request, *args, **kwargs):
        return subtitles_response(self.get_object())


class JobVideoView(JobDetailView):
    def get(self, request, *args, **kwargs):
        return video_response(
            self.get_object(), attachment=True,
            range_header=request.headers.get('Range') if 'If-Range' not in request.headers else None,
        )


class OperationsView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response(capacity_snapshot())
