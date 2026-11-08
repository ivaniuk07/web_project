"""Worker liveness is shared through PostgreSQL across separate Redis brokers."""
import logging

from celery.signals import heartbeat_sent, worker_ready, worker_shutdown
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .models import WorkerNode

logger = logging.getLogger(__name__)


def heartbeat(**kwargs):
    close_old_connections()
    try:
        if not WorkerNode.objects.filter(name=settings.WORKER_NODE_NAME).exists():
            config = next((node for node in settings.WORKER_NODES if node['name'] == settings.WORKER_NODE_NAME), None)
            if config is None:
                return
            WorkerNode.objects.get_or_create(name=config['name'], defaults={
                'broker_url': config['broker_url'], 'queue': config['queue'],
                'enabled': config.get('enabled', True),
            })
        WorkerNode.objects.filter(name=settings.WORKER_NODE_NAME).update(heartbeat_at=timezone.now())
    except Exception:
        logger.exception('Worker heartbeat could not be saved')


@worker_ready.connect
def register_worker(**kwargs):
    heartbeat()


@worker_shutdown.connect
def unregister_worker(**kwargs):
    WorkerNode.objects.filter(name=settings.WORKER_NODE_NAME).update(heartbeat_at=None)


heartbeat_sent.connect(heartbeat, weak=False)
