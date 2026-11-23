from typing import Protocol

from app.schemas.regression_specs import RegressionRequest, RegressionResult


class RegressionGenerator(Protocol):
    def generate(self, request: RegressionRequest) -> RegressionResult: ...
