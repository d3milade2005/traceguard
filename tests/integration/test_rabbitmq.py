import pytest

from app.integrations.broker import check_broker

pytestmark = pytest.mark.integration


def test_authenticated_rabbitmq_connection(integration_settings):
    if integration_settings.test_rabbitmq_url is None:
        pytest.skip("Set TEST_RABBITMQ_URL to run RabbitMQ integration tests")
    check_broker(integration_settings.test_rabbitmq_url.get_secret_value(), timeout=3)
