"""Persistent, bounded exchange-rate cache; no HTTP call runs in a DB transaction."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging

from sqlalchemy.exc import SQLAlchemyError

from app.models import BotSettings
from app.services.manual_payments import CRYPTO_ASSETS
from app.services.rates import CoinGeckoRateProvider, RateSnapshot, RateUnavailable

log = logging.getLogger(__name__)
CACHE_KEY = "crypto_rate_cache_v1"


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class RateCache:
    def __init__(self, db, settings, provider=None, clock=None):
        self.db = db
        self.settings = settings
        self.provider = provider or CoinGeckoRateProvider(api_key=settings.coingecko_api_key)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._snapshots = {}
        self._next_attempt = datetime.min.replace(tzinfo=timezone.utc)

    def _valid(self, snapshots):
        if not isinstance(snapshots, dict) or set(snapshots) != set(CRYPTO_ASSETS):
            return False
        now = utc(self.clock())
        for asset, snapshot in snapshots.items():
            if not isinstance(snapshot, RateSnapshot) or snapshot.asset != asset:
                return False
            for value in (snapshot.price_pln, snapshot.low_24h):
                if not isinstance(value, Decimal) or not value.is_finite() or not Decimal("1e-18") <= value <= Decimal("1e15"):
                    return False
            if not isinstance(snapshot.source, str) or not 1 <= len(snapshot.source) <= 100:
                return False
            if not isinstance(snapshot.source_updated_at, datetime) or not isinstance(snapshot.fetched_at, datetime):
                return False
            if snapshot.source_updated_at.tzinfo is None or snapshot.fetched_at.tzinfo is None:
                return False
            if utc(snapshot.source_updated_at) > now + timedelta(minutes=5) or utc(snapshot.fetched_at) > now + timedelta(minutes=5):
                return False
        return True

    def _fresh(self, snapshots):
        if not self._valid(snapshots):
            return False
        cutoff = utc(self.clock()) - timedelta(seconds=self.settings.rate_max_age_seconds)
        return all(utc(s.source_updated_at) > cutoff and utc(s.fetched_at) > cutoff for s in snapshots.values())

    @staticmethod
    def _batch_time(snapshots):
        return min(utc(s.fetched_at) for s in snapshots.values())

    @staticmethod
    def _encode(snapshots):
        return json.dumps({"version": 1, "snapshots": {
            code: {"asset": s.asset, "price_pln": str(s.price_pln), "low_24h": str(s.low_24h),
                   "source": s.source, "source_updated_at": utc(s.source_updated_at).isoformat(),
                   "fetched_at": utc(s.fetched_at).isoformat()}
            for code, s in snapshots.items()
        }}, separators=(",", ":"))

    def _decode(self, raw):
        if not isinstance(raw, str) or len(raw) > 32_768:
            return {}
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("snapshots"), dict):
                return {}
            snapshots = {}
            for code, item in value["snapshots"].items():
                if not isinstance(item, dict) or not isinstance(item.get("price_pln"), str) or not isinstance(item.get("low_24h"), str):
                    return {}
                snapshots[code] = RateSnapshot(
                    asset=item["asset"], price_pln=Decimal(item["price_pln"]), low_24h=Decimal(item["low_24h"]),
                    source=item["source"], source_updated_at=datetime.fromisoformat(item["source_updated_at"]),
                    fetched_at=datetime.fromisoformat(item["fetched_at"]),
                )
            return snapshots if self._valid(snapshots) else {}
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError, RecursionError):
            return {}

    async def _load(self):
        async with self.db.sessions() as session:
            row = await session.get(BotSettings, CACHE_KEY)
            stored = self._decode(row.value) if row else {}
        if stored and (not self._snapshots or self._batch_time(stored) >= self._batch_time(self._snapshots)):
            self._snapshots = stored

    async def _save(self, snapshots):
        async with self.db.write() as session:
            row = await session.get(BotSettings, CACHE_KEY)
            stored = self._decode(row.value) if row else {}
            # Another process may have completed a newer refresh while this one
            # waited for its HTTP response. Never overwrite that newer batch.
            if stored and self._batch_time(stored) > self._batch_time(snapshots):
                return stored
            encoded = self._encode(snapshots)
            if row:
                row.value = encoded
            else:
                session.add(BotSettings(key=CACHE_KEY, value=encoded))
        return snapshots

    async def refresh(self, force=False):
        async with self._lock:
            await self._load()
            now = utc(self.clock())
            due = not self._fresh(self._snapshots) or (
                now - self._batch_time(self._snapshots)
            ).total_seconds() >= self.settings.rate_refresh_seconds
            if not force and not due:
                return dict(self._snapshots)
            if now < self._next_attempt:
                if self._fresh(self._snapshots):
                    return dict(self._snapshots)
                raise RateUnavailable("Kursy są chwilowo niedostępne. Spróbuj ponownie za minutę.")
            try:
                snapshots = await self.provider.fetch()
                if not self._fresh(snapshots):
                    raise RateUnavailable("Dostawca nie zwrócił kompletnych, aktualnych kursów.")
                self._snapshots = await self._save(snapshots)
                self._next_attempt = datetime.min.replace(tzinfo=timezone.utc)
                return dict(self._snapshots)
            except RateUnavailable:
                self._next_attempt = now + timedelta(seconds=60)
                log.warning("Nie udało się odświeżyć kursów kryptowalut.")
                if self._fresh(self._snapshots):
                    return dict(self._snapshots)
                raise RateUnavailable("Brak aktualnego kursu. Wycena wpłaty jest chwilowo niedostępna; spróbuj później.") from None

    async def get(self, asset):
        if asset not in CRYPTO_ASSETS:
            raise RateUnavailable("Nieobsługiwana kryptowaluta.")
        snapshots = await self.refresh()
        if not self._fresh(snapshots):
            raise RateUnavailable("Kurs wygasł. Nie twórz wpłaty do czasu jego odświeżenia.")
        return snapshots[asset]

    async def run(self, stop_event):
        while not stop_event.is_set():
            try:
                await self.refresh()
            except RateUnavailable:
                pass
            except SQLAlchemyError as exc:
                log.error("Nie można zapisać/odczytać kursów (%s).", type(exc).__name__)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=min(60, self.settings.rate_refresh_seconds))
            except TimeoutError:
                pass
