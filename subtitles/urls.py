from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('jobs/<uuid:job_id>/', views.job_detail, name='job_detail'),
    path('process/<uuid:job_id>/', views.process_video, name='process_video'),
    path('download/<uuid:job_id>/', views.download_subtitles, name='download_subtitles'),
    path('video/<uuid:job_id>/', views.video_result, name='video_result'),
]
