import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.manual_payments import CRYPTO_ASSETS, crypto_amount


def test_supported_currencies_have_explicit_networks_and_precision():
    assert set(CRYPTO_ASSETS) == {"BTC", "ETH", "USDC", "SOL", "LTC", "XMR"}
    assert "ERC-20" in CRYPTO_ASSETS["USDC"]["network"]
    assert CRYPTO_ASSETS["ETH"]["decimals"] == 18
    assert CRYPTO_ASSETS["USDC"]["decimals"] == 6


@pytest.mark.parametrize("field,value", [
    ("crypto_eth_address", "0x" + "0" * 40),
    ("crypto_usdc_address", "not-a-wallet"),
    ("crypto_btc_address", "0x" + "1" * 40),
    ("crypto_sol_address", "bc1qexample"),
])
def test_incorrect_destination_is_rejected_by_configuration(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_exact_crypto_precision_without_float_rounding():
    assert crypto_amount("ETH", "0.000000000000000001") == "0.000000000000000001"
    assert crypto_amount("USDC", "25,123456") == "25.123456"
    with pytest.raises(ValueError):
        crypto_amount("USDC", "25.1234567")
