import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize("value", ["123,456", "[123, 456]"])
def test_admin_ids_accept_csv_and_json(value):
    settings = Settings(_env_file=None, admin_ids=value)
    assert settings.admin_ids == [123, 456]
    assert settings.is_admin(123)
    assert not settings.is_admin(789)


def test_manual_real_money_mode_is_default_and_demo_remains_explicit():
    assert Settings(_env_file=None).demo_mode is False
    assert Settings(_env_file=None, demo_mode=False).demo_mode is False
    assert Settings(_env_file=None, demo_mode=True).demo_mode is True


def test_token_is_not_in_configuration_repr():
    settings = Settings(_env_file=None, bot_token="secret-test-token")
    assert "secret-test-token" not in repr(settings)


def test_merchant_contact_is_normalized_and_not_an_arbitrary_url():
    assert Settings(_env_file=None, merchant_telegram_username="@monstal_owner").merchant_telegram_username == "monstal_owner"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, merchant_telegram_username="https://example.org/not-telegram")
