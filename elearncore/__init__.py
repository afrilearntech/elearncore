try:
	from .celery import app as celery_app
except Exception:  # pragma: no cover - keep Django startup working before Celery is installed
	celery_app = None

__all__ = ('celery_app',)
