from app.core.config import get_settings
from app.workers.factory import create_celery_app

celery_app = create_celery_app(get_settings(), validation_worker=True)
