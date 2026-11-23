import logging

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError

logger = logging.getLogger(__name__)


def create_database_engine(settings: Settings) -> Engine:
    url = make_url(settings.database_url.get_secret_value())
    options = str(url.query.get("options", ""))
    statement_timeout = settings.dependency_timeout_seconds * 1000
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.dependency_timeout_seconds,
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.dependency_timeout_seconds,
            "options": f"{options} -c statement_timeout={statement_timeout}",
        },
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # Services own `with sessions.begin()`; dependencies never commit implicitly.
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def check_database(engine: Engine) -> None:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.warning("Database readiness failed", exc_info=True, extra={"component": "database"})
        raise DependencyUnavailableError from exc
