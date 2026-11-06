from django.urls import path
from .views import home, process_video, download_subtitles, video_result

urlpatterns = [
    path('', home, name='home'),
    path('process/<str:filename>/', process_video, name='process_video'),
    path('download/<str:filename>/', download_subtitles, name='download_subtitles'),
    path('video/<str:filename>/', video_result, name='video_result'),
]
