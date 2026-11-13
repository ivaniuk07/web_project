from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import TemplateView
from subtitles.views import register

urlpatterns = [
    path('favicon.svg', TemplateView.as_view(template_name='studio/favicon.svg', content_type='image/svg+xml'), name='favicon'),
    path('accounts/register/', register, name='register'),
    path('accounts/login/', auth_views.LoginView.as_view(redirect_authenticated_user=True), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('api/', include('subtitles.api_urls')),
    path('', include('subtitles.urls')),
]
