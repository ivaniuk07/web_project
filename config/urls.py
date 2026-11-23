from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import TemplateView
from subtitles import views

urlpatterns = [
    path('favicon.svg', TemplateView.as_view(template_name='studio/favicon.svg', content_type='image/svg+xml'), name='favicon'),
    path('accounts/register/', views.register, name='register'),
    path('accounts/login/', auth_views.LoginView.as_view(redirect_authenticated_user=True), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('operations/', views.system_status, name='operations'),
    path('', views.home, name='home'),
    path('history/', views.job_history, name='job_history'),
    path('jobs/<uuid:job_id>/', views.job_detail, name='job_detail'),
    path('process/<uuid:job_id>/', views.process_video, name='process_video'),
    path('cancel/<uuid:job_id>/', views.cancel_job, name='cancel_job'),
    path('download/<uuid:job_id>/', views.download_subtitles, name='download_subtitles'),
    path('video/<uuid:job_id>/', views.video_result, name='video_result'),
]
