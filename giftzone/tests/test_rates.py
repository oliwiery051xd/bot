from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json

import pytest

from app.services.manual_payments import CRYPTO_ASSETS
from app.services.rates import (
    COINGECKO_IDS, CoinGeckoRateProvider, MAX_RESPONSE_BYTES, RateUnavailable,
    crypto_for_pln, parse_market_snapshots, select_price,
)


NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def payload():
    return [{"id": coin, "current_price": "12345.123456789012345678", "low_24h": "12000.123456789012345678", "last_updated": (NOW - timedelta(minutes=3)).isoformat()} for coin in COINGECKO_IDS]


def body(entries=None):
    return json.dumps(payload() if entries is None else entries).encode()


def test_snapshot_has_all_assets_exact_decimal_source_and_times():
    result = parse_market_snapshots(body(), fetched_at=NOW)
    assert set(result) == set(CRYPTO_ASSETS)
    snapshot = result["ETH"]
    assert snapshot.price_pln == Decimal("12345.123456789012345678")
    assert snapshot.low_24h == Decimal("12000.123456789012345678")
    assert snapshot.source == "CoinGecko · PLN"
    assert snapshot.source_updated_at == NOW - timedelta(minutes=3)
    assert snapshot.fetched_at == NOW
    assert select_price(snapshot) == snapshot.price_pln
    assert select_price(snapshot, "low_24h") == snapshot.low_24h
    with pytest.raises(ValueError):
        select_price(snapshot, "unknown")


def test_json_numeric_decimals_do_not_pass_through_binary_float():
    raw = body().replace(b'"12345.123456789012345678"', b'12345.123456789012345678')
    assert parse_market_snapshots(raw, fetched_at=NOW)["ETH"].price_pln == Decimal("12345.123456789012345678")


@pytest.mark.parametrize("bad", [b"", b"invalid", b"{}", b"[]", b"[NaN]", b"\xff", b"x" * (MAX_RESPONSE_BYTES + 1)])
def test_bad_provider_bodies_are_safe_errors(bad):
    with pytest.raises(RateUnavailable):
        parse_market_snapshots(bad, fetched_at=NOW)


@pytest.mark.parametrize("field,value", [
    ("current_price", 0), ("current_price", -1), ("current_price", True),
    ("current_price", "NaN"), ("current_price", "Infinity"), ("current_price", "1e-100"),
    ("current_price", "1e100"), ("low_24h", None), ("low_24h", 0),
    ("last_updated", None), ("last_updated", "2026-11-03T11:59:00"),
    ("last_updated", (NOW - timedelta(hours=2)).isoformat()),
    ("last_updated", (NOW + timedelta(minutes=6)).isoformat()),
])
def test_invalid_quote_fields_reject_the_entire_fetch(field, value):
    entries = payload()
    entries[0][field] = value
    with pytest.raises(RateUnavailable):
        parse_market_snapshots(body(entries), fetched_at=NOW)


def test_partial_duplicate_and_unexpected_coins_reject_the_entire_fetch():
    for entries in (payload()[:-1], payload() + payload()[:1]):
        with pytest.raises(RateUnavailable):
            parse_market_snapshots(body(entries), fetched_at=NOW)
    entries = payload()
    entries[-1] = entries[0]
    with pytest.raises(RateUnavailable):
        parse_market_snapshots(body(entries), fetched_at=NOW)
    entries[-1] = {"id": "not-an-approved-coin"}
    with pytest.raises(RateUnavailable):
        parse_market_snapshots(body(entries), fetched_at=NOW)


@pytest.mark.parametrize("asset", list(CRYPTO_ASSETS))
def test_crypto_conversion_rounds_up_minimum_atomic_unit(asset):
    price = Decimal("12345.987654321")
    result = Decimal(crypto_for_pln(5000, price, asset))
    quantum = Decimal(1).scaleb(-CRYPTO_ASSETS[asset]["decimals"])
    with localcontext() as context:
        context.prec = 80
        assert result * price >= Decimal(50)
        assert (result - quantum) * price < Decimal(50)
    assert result.as_tuple().exponent == -CRYPTO_ASSETS[asset]["decimals"]


def test_fifty_pln_eth_example_uses_exact_rate_not_manual_value():
    assert crypto_for_pln(5000, Decimal("10000"), "ETH") == "0.005000000000000000"
    assert crypto_for_pln(5000, Decimal("5000"), "ETH") == "0.010000000000000000"


@pytest.mark.parametrize("grosze,price,asset", [(True, Decimal(1), "BTC"), (0, Decimal(1), "ETH"), (-1, Decimal(1), "BTC"), (100_000_001, Decimal(1), "BTC"), (1, Decimal(0), "ETH"), (1, Decimal("NaN"), "ETH"), (5000, 123.45, "ETH"), (5000, Decimal(1), "UNKNOWN"), (1, Decimal("1e-18"), "BTC")])
def test_invalid_amounts_cannot_produce_payment_instructions(grosze, price, asset):
    with pytest.raises(ValueError):
        crypto_for_pln(grosze, price, asset)


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, raw=None, content_length=None):
        self.status = status
        self.content_length = content_length
        self.content = FakeContent([body() if raw is None else raw])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    response = None
    options = None
    request = None
    failure = None

    def __init__(self, **kwargs):
        type(self).options = kwargs
        self.connector = kwargs["connector"]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.connector.close()
        return False

    def get(self, url, **kwargs):
        type(self).request = (url, kwargs)
        if self.failure:
            raise self.failure
        return self.response


@pytest.mark.parametrize("status", [301, 403, 429, 500])
async def test_http_errors_are_safe_without_leaking_key(monkeypatch, status):
    FakeSession.response = FakeResponse(status=status)
    FakeSession.failure = None
    monkeypatch.setattr("app.services.rates.aiohttp.ClientSession", FakeSession)
    with pytest.raises(RateUnavailable) as error:
        await CoinGeckoRateProvider(api_key="sensitive-test-key").fetch()
    assert "sensitive-test-key" not in str(error.value)
    assert FakeSession.options["trust_env"] is True
    assert FakeSession.request[1]["allow_redirects"] is False
    assert FakeSession.request[1]["headers"]["x-cg-demo-api-key"] == "sensitive-test-key"


async def test_fetch_checks_declared_size_and_streamed_size(monkeypatch):
    monkeypatch.setattr("app.services.rates.aiohttp.ClientSession", FakeSession)
    FakeSession.failure = None
    FakeSession.response = FakeResponse(content_length=MAX_RESPONSE_BYTES + 1)
    with pytest.raises(RateUnavailable):
        await CoinGeckoRateProvider().fetch()
    FakeSession.response = FakeResponse(raw=b"x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(RateUnavailable):
        await CoinGeckoRateProvider().fetch()


async def test_timeout_is_safe_and_bounded(monkeypatch):
    monkeypatch.setattr("app.services.rates.aiohttp.ClientSession", FakeSession)
    FakeSession.failure = TimeoutError("sensitive-test-key")
    try:
        with pytest.raises(RateUnavailable) as error:
            await CoinGeckoRateProvider(api_key="sensitive-test-key").fetch()
        assert "sensitive-test-key" not in str(error.value)
        assert FakeSession.options["timeout"].total == 10
    finally:
        FakeSession.failure = None


async def test_successful_fetch_uses_fixed_pln_source_and_verified_tls(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    import ssl
    monkeypatch.setattr("app.services.rates.datetime", FrozenDatetime)
    monkeypatch.setattr("app.services.rates.aiohttp.ClientSession", FakeSession)
    FakeSession.failure = None
    FakeSession.response = FakeResponse()
    result = await CoinGeckoRateProvider().fetch()
    assert set(result) == set(CRYPTO_ASSETS)
    assert result["USDC"].price_pln == Decimal("12345.123456789012345678")
    assert FakeSession.request[0] == "https://api.coingecko.com/api/v3/coins/markets"
    assert FakeSession.request[1]["params"]["vs_currency"] == "pln"
    assert set(FakeSession.request[1]["params"]["ids"].split(",")) == set(COINGECKO_IDS)
    assert "x-cg-demo-api-key" not in FakeSession.request[1]["headers"]
    assert FakeSession.options["connector"]._ssl.verify_mode == ssl.CERT_REQUIRED
    assert FakeSession.options["connector"]._ssl.check_hostname is True


def test_json_nesting_error_is_safe():
    with pytest.raises(RateUnavailable):
        parse_market_snapshots(b"[" * 2000 + b"]" * 2000, fetched_at=NOW)
