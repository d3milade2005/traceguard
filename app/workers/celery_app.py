from app.core.config import get_settings
from app.workers.factory import create_celery_app as create_celery_app

# Explicit CLI entry point; API lifecycle owns its separate publisher instance.
celery_app = create_celery_app(get_settings())
