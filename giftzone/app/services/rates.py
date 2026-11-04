"""Exact PLN market snapshots from CoinGecko; no transfers or wallet access.

Fetch outside a database transaction. A caller must persist snapshots, enforce
its chosen cache lifetime and pin a specific snapshot to each payment quote.
The 24-hour low is a provider-reported rolling minimum, not a future price or a
guarantee of the execution price at any exchange.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import json
import ssl

import aiohttp

from app.services.manual_payments import CRYPTO_ASSETS


COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"
COINGECKO_IDS = {
    "bitcoin": "BTC", "ethereum": "ETH", "usd-coin": "USDC",
    "solana": "SOL", "litecoin": "LTC", "monero": "XMR",
}
SOURCE_NAME = "CoinGecko · PLN"
MAX_RESPONSE_BYTES = 256 * 1024
MAX_PROVIDER_AGE = timedelta(hours=1)
MAX_FUTURE_SKEW = timedelta(minutes=5)
MIN_PRICE = Decimal("0.000000000000000001")
MAX_PRICE = Decimal("1000000000000000")


class RateUnavailable(Exception):
    """A safe message, with no HTTP response body, URL token or credentials."""


@dataclass(frozen=True, slots=True)
class RateSnapshot:
    asset: str
    price_pln: Decimal
    low_24h: Decimal
    source: str
    source_updated_at: datetime
    fetched_at: datetime


def _price(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Nieprawidłowy format kursu.")
    if isinstance(value, str) and len(value) > 100:
        raise ValueError("Nieprawidłowy format kursu.")
    try:
        result = Decimal(value)
        if not result.is_finite() or not MIN_PRICE <= result <= MAX_PRICE:
            raise ValueError("Kurs poza dopuszczalnym zakresem.")
        return result
    except InvalidOperation:
        raise ValueError("Nieprawidłowy format kursu.") from None


def select_price(snapshot: RateSnapshot, mode: str = "current") -> Decimal:
    """Select explicitly; never silently replace an unavailable daily low."""
    if mode == "current":
        return _price(snapshot.price_pln)
    if mode == "low_24h":
        return _price(snapshot.low_24h)
    raise ValueError("Wybierz tryb kursu current albo low_24h.")


def crypto_for_pln(grosze: int, price: Decimal, asset: str) -> str:
    """Return an exact decimal amount, rounded up by at most one atomic unit."""
    if type(grosze) is not int or not 1 <= grosze <= 100_000_000:
        raise ValueError("Nieprawidłowa kwota PLN do wyceny.")
    if asset not in CRYPTO_ASSETS:
        raise ValueError("Nieobsługiwana kryptowaluta.")
    rate = _price(price)
    try:
        with localcontext() as ctx:
            ctx.prec = 80
            pln = Decimal(grosze) / Decimal(100)
            quantum = Decimal(1).scaleb(-CRYPTO_ASSETS[asset]["decimals"])
            result = (pln / rate).quantize(quantum, rounding=ROUND_CEILING)
            if not 0 < result <= Decimal("1000000000"):
                raise ValueError("Wyliczona ilość kryptowaluty jest poza dopuszczalnym zakresem.")
            return format(result, "f")
    except InvalidOperation:
        raise ValueError("Nie można bezpiecznie wyliczyć kwoty kryptowaluty.") from None


def _reject_constant(_):
    raise ValueError("Niedozwolona wartość JSON.")


def parse_market_snapshots(body: bytes, *, fetched_at: datetime | None = None) -> dict[str, RateSnapshot]:
    """Validate the entire snapshot before it replaces a previously valid cache."""
    now = fetched_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Czas pobrania kursu musi zawierać strefę czasową.")
    now = now.astimezone(timezone.utc)
    if not body or len(body) > MAX_RESPONSE_BYTES:
        raise RateUnavailable("Źródło kursów zwróciło nieprawidłowe dane. Spróbuj ponownie później.")
    try:
        payload = json.loads(body.decode("utf-8"), parse_float=Decimal, parse_constant=_reject_constant)
        if not isinstance(payload, list) or len(payload) != len(COINGECKO_IDS):
            raise ValueError("Niepełna odpowiedź źródła kursów.")
        snapshots = {}
        for entry in payload:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or entry["id"] not in COINGECKO_IDS:
                raise ValueError("Nieoczekiwana waluta w źródle kursów.")
            asset = COINGECKO_IDS[entry["id"]]
            if asset in snapshots:
                raise ValueError("Powtórzona waluta w źródle kursów.")
            raw_timestamp = entry.get("last_updated")
            if not isinstance(raw_timestamp, str) or len(raw_timestamp) > 80:
                raise ValueError("Brak czasu aktualizacji źródła.")
            updated = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            if updated.tzinfo is None:
                raise ValueError("Nieprawidłowy czas aktualizacji źródła.")
            updated = updated.astimezone(timezone.utc)
            if now - updated > MAX_PROVIDER_AGE or updated - now > MAX_FUTURE_SKEW:
                raise ValueError("Nieaktualny czas źródła kursów.")
            snapshots[asset] = RateSnapshot(
                asset=asset, price_pln=_price(entry.get("current_price")),
                low_24h=_price(entry.get("low_24h")), source=SOURCE_NAME,
                source_updated_at=updated, fetched_at=now,
            )
        if set(snapshots) != set(COINGECKO_IDS.values()):
            raise ValueError("Niepełna odpowiedź źródła kursów.")
        return snapshots
    except (ValueError, TypeError, OverflowError, InvalidOperation, RecursionError):
        raise RateUnavailable("Źródło kursów zwróciło niepełne, nieaktualne lub nieprawidłowe dane. Spróbuj ponownie później.") from None


class CoinGeckoRateProvider:
    def __init__(self, api_key: str = ""):
        self._api_key = api_key

    async def fetch(self) -> dict[str, RateSnapshot]:
        headers = {"Accept": "application/json", "User-Agent": "MONSTAL/2.0"}
        if self._api_key:
            headers["x-cg-demo-api-key"] = self._api_key
        params = {"vs_currency": "pln", "ids": ",".join(COINGECKO_IDS), "per_page": "100", "page": "1"}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10), trust_env=True,
                connector=aiohttp.TCPConnector(ssl=ssl.create_default_context()),
            ) as session:
                async with session.get(COINGECKO_URL, params=params, headers=headers, allow_redirects=False) as response:
                    if response.status == 429:
                        raise RateUnavailable("Źródło kursów ograniczyło liczbę zapytań. Spróbuj ponownie później.")
                    if response.status != 200:
                        raise RateUnavailable("Źródło kursów jest chwilowo niedostępne. Spróbuj ponownie później.")
                    if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                        raise RateUnavailable("Źródło kursów zwróciło zbyt dużą odpowiedź.")
                    content = bytearray()
                    async for chunk in response.content.iter_chunked(16 * 1024):
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise RateUnavailable("Źródło kursów zwróciło zbyt dużą odpowiedź.")
                    return parse_market_snapshots(bytes(content))
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise RateUnavailable("Nie udało się bezpiecznie pobrać kursów. Spróbuj ponownie później.") from None
