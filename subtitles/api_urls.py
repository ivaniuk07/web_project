from django.urls import path

from .api import JobCreateView, JobDetailView, JobSubtitlesView, JobVideoView, OperationsView

app_name = 'jobs-api'
urlpatterns = [
    path('jobs/', JobCreateView.as_view(), name='create'),
    path('jobs/<uuid:pk>/', JobDetailView.as_view(), name='detail'),
    path('jobs/<uuid:pk>/subtitles/', JobSubtitlesView.as_view(), name='subtitles'),
    path('jobs/<uuid:pk>/video/', JobVideoView.as_view(), name='video'),
    path('operations/', OperationsView.as_view(), name='operations'),
]
