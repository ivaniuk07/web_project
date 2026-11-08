import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-local-development-only')
DEBUG = os.environ.get('DJANGO_DEBUG', 'true').lower() == 'true'
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
    if host.strip()
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'subtitles',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
    ]},
}]
WSGI_APPLICATION = 'config.wsgi.application'
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'web_project'),
        'USER': os.environ.get('POSTGRES_USER', os.environ.get('USER', 'postgres')),
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
        'HOST': os.environ.get('POSTGRES_HOST', '127.0.0.1'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
        'OPTIONS': {'connect_timeout': 5},
    },
}
LANGUAGE_CODE = 'uk'
TIME_ZONE = 'Europe/Kyiv'
USE_I18N = True
USE_TZ = True
STATIC_URL = 'static/'
MEDIA_ROOT = Path(os.environ.get('MEDIA_ROOT', str(BASE_DIR / 'media')))
MAX_UPLOAD_BYTES = int(os.environ.get('MAX_UPLOAD_BYTES', 100 * 1024 * 1024))
ALLOWED_VIDEO_EXTENSIONS = [
    value.strip().lower().lstrip('.')
    for value in os.environ.get('ALLOWED_VIDEO_EXTENSIONS', 'mp4,mov,mkv,webm,avi').split(',')
    if value.strip()
]
MAX_ACTIVE_JOBS_PER_USER = int(os.environ.get('MAX_ACTIVE_JOBS_PER_USER', 3))
JOB_TIMEOUT_SECONDS = float(os.environ.get('JOB_TIMEOUT_SECONDS', 900))
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

MAX_VIDEO_SECONDS = int(os.environ.get('MAX_VIDEO_SECONDS', 600))
MEDIA_PROBE_TIMEOUT = int(os.environ.get('MEDIA_PROBE_TIMEOUT', 20))
FFMPEG_TIMEOUT = int(os.environ.get('FFMPEG_TIMEOUT', 120))
FFMPEG_BINARY = os.environ.get('FFMPEG_BINARY', 'ffmpeg')
VIDEO_RENDER_TIMEOUT = int(os.environ.get('VIDEO_RENDER_TIMEOUT', 600))
TRANSCRIBE_TIMEOUT = int(os.environ.get('TRANSCRIBE_TIMEOUT', 600))
WHISPER_MODEL = os.environ.get('WHISPER_MODEL', 'tiny')

CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_TASK_IGNORE_RESULT = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_DEFAULT_QUEUE = os.environ.get('CELERY_TASK_DEFAULT_QUEUE', 'subtitles-local')
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_TIMEOUT = 2
CELERY_BROKER_TRANSPORT_OPTIONS = {'socket_connect_timeout': 2, 'socket_timeout': 2, 'visibility_timeout': int(JOB_TIMEOUT_SECONDS) + 300}
WORKER_NODE_NAME = os.environ.get('WORKER_NODE_NAME', 'node-1')
WORKER_NODES = json.loads(os.environ.get('WORKER_NODES', json.dumps([
    {'name': 'node-1', 'broker_url': 'redis://localhost:6379/0', 'queue': CELERY_TASK_DEFAULT_QUEUE},
    {'name': 'node-2', 'broker_url': 'redis://localhost:6380/0', 'queue': CELERY_TASK_DEFAULT_QUEUE},
])))
NODE_TIMEOUT_SECONDS = float(os.environ.get('NODE_TIMEOUT_SECONDS', 20))
NODE_RETRY_SECONDS = float(os.environ.get('NODE_RETRY_SECONDS', 10))
DISPATCH_INTERVAL_SECONDS = float(os.environ.get('DISPATCH_INTERVAL_SECONDS', 2))
DISPATCH_RESERVATION_SECONDS = float(os.environ.get('DISPATCH_RESERVATION_SECONDS', 15))
MAX_DISPATCH_ATTEMPTS = int(os.environ.get('MAX_DISPATCH_ATTEMPTS', 5))

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'login'
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
