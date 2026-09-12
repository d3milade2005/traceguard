from sqlalchemy import Engine

from app.db.session import check_database
from app.integrations.broker import check_broker


class ReadinessService:
    def __init__(self, engine: Engine, broker_url: str, timeout: int) -> None:
        self.engine = engine
        self.broker_url = broker_url
        self.timeout = timeout

    def check(self) -> None:
        check_database(self.engine)
        check_broker(self.broker_url, self.timeout)
