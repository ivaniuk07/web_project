from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('jobs/<uuid:job_id>/', views.job_detail, name='job_detail'),
    path('process/<uuid:job_id>/', views.process_video, name='process_video'),
    path('cancel/<uuid:job_id>/', views.cancel_job, name='cancel_job'),
    path('download/<uuid:job_id>/', views.download_subtitles, name='download_subtitles'),
    path('video/<uuid:job_id>/', views.video_result, name='video_result'),
]
