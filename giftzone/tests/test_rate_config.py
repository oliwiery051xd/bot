import pytest
from pydantic import ValidationError
from app.config import Settings


@pytest.mark.parametrize("fields", [
    {"rate_price_mode": "lowest-ever"},
    {"rate_refresh_seconds": 0},
    {"rate_refresh_seconds": 86401},
    {"rate_max_age_seconds": 0},
    {"rate_max_age_seconds": 86401},
])
def test_rate_configuration_rejects_unsafe_or_unknown_options(fields):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **fields)


def test_optional_rate_api_key_is_hidden_from_repr():
    assert "private-test-key" not in repr(Settings(_env_file=None, coingecko_api_key="private-test-key"))
