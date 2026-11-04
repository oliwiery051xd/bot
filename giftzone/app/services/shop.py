"""Transactional MONSTAL use cases, independent of Telegram transport.

All state-changing operations run in Database.write(): a database-scoped lock
serializes money/stock/referral changes across processes. Durable request keys and
ledger uniqueness preserve idempotence after retries and process restarts.
"""
from __future__ import annotations

import json
import re
import hashlib
import hmac
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # DEMO remains usable; real shipping collection fails closed.
    Fernet = None
    InvalidToken = ValueError

from app.models import (
    AdminAuditLog, BalanceTransaction, BotSettings, Category, DemoTopUp,
    Order, Product, Referral, User,
)
from app.repositories.users import by_telegram_id
from app.services.payments import DemoPaymentProvider
from app.services.manual_payments import CRYPTO_ASSETS, crypto_amount as normalize_crypto_amount, validate_address
from app.services.rate_cache import RateCache
from app.services.rates import RateUnavailable, crypto_for_pln, select_price


class DomainError(Exception):
    """Safe, user-facing validation or business rule error."""


class ShopService:
    def __init__(self, db, settings, rate_cache=None):
        self.db = db
        self.settings = settings
        self.payments = DemoPaymentProvider()
        self.rate_cache = rate_cache or RateCache(db, settings)

    @property
    def balance_mode(self):
        return "demo" if self.settings.demo_mode else "real"

    async def _check_mode(self, s, *, initialize=False):
        marker = await s.get(BotSettings, "balance_mode")
        if marker is None:
            # An older schema had DEMO-only records. Never relabel those funds.
            legacy = await s.get(BotSettings, "seed_version")
            populated = legacy is not None or any([
                await s.scalar(select(User.id).limit(1)) is not None,
                await s.scalar(select(Order.id).limit(1)) is not None,
                await s.scalar(select(DemoTopUp.id).limit(1)) is not None,
                await s.scalar(select(BalanceTransaction.id).limit(1)) is not None,
                await s.scalar(select(Product.id).limit(1)) is not None,
            ])
            recorded = "demo" if populated else self.balance_mode
            if recorded != self.balance_mode:
                raise DomainError("Ta baza zawiera dane DEMO. Ustaw DEMO_MODE=true albo wybierz nową bazę monstal.db dla prawdziwych sald. Salda DEMO nie są prawdziwymi pieniędzmi.")
            if initialize:
                s.add(BotSettings(key="balance_mode", value=recorded))
                await s.flush()
            return
        if marker.value != self.balance_mode:
            raise DomainError("Tryb salda tej bazy jest inny niż DEMO_MODE. Wybierz oddzielną bazę dla DEMO i prawdziwych wpłat; nie przeliczamy ani nie konwertujemy sald.")

    @asynccontextmanager
    async def _write(self):
        async with self.db.write() as s:
            await self._check_mode(s, initialize=True)
            yield s

    @asynccontextmanager
    async def _read(self):
        await self.ensure_mode()
        async with self.db.sessions() as s:
            await self._check_mode(s)
            yield s

    async def ensure_mode(self):
        async with self._write():
            return self.balance_mode

    def crypto_options(self):
        return [{"code": code, **metadata,
                 "address": getattr(self.settings, metadata["setting"]),
                 "configured": bool(getattr(self.settings, metadata["setting"]))}
                for code, metadata in CRYPTO_ASSETS.items()]

    @staticmethod
    def _utc(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @classmethod
    def quote_token(cls, topup):
        if topup.crypto_amount is None or topup.quote_expires_at is None:
            return ""
        version = f"{topup.id}|{topup.crypto_amount}|{cls._utc(topup.quote_expires_at).isoformat()}"
        return hashlib.sha256(version.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _confirmation_reference(topup, value):
        if not isinstance(value, str):
            raise DomainError("Podaj identyfikator rzeczywiście sprawdzonej wpłaty. Nie zapisuj kodów BLIK.")
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{3,140}", value) or re.fullmatch(r"[0-9]{6}", value):
            raise DomainError("Podaj identyfikator potwierdzenia lub transakcji (3–140 znaków). Kod BLIK nie jest potwierdzeniem i nie może być zapisany.")
        if topup.method == "crypto":
            if topup.asset == "SOL":
                if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,88}", value):
                    raise DomainError("Podaj identyfikator transakcji Solana w formacie Base58 (64–88 znaków).")
            else:
                if topup.asset in {"ETH", "USDC"}:
                    value = value.removeprefix("0x").removeprefix("0X")
                if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
                    raise DomainError("Podaj 64-znakowy hash rzeczywiście sprawdzonej transakcji. Nie podawaj adresu portfela ani linku.")
                value = value.lower()
        else:
            value = value.lower()
        chain = "ETHEREUM" if topup.asset in {"ETH", "USDC"} else topup.asset if topup.method == "crypto" else "BLIK"
        # ETH and USDC share the same chain, so a transaction cannot fund both.
        return f"{chain}:{value}"

    def _admin(self, telegram_id: int):
        if not self.settings.is_admin(telegram_id):
            raise DomainError("Brak uprawnień administratora.")

    @staticmethod
    def _key(key: str):
        if not isinstance(key, str) or not 8 <= len(key) <= 100 or not re.fullmatch(r"[A-Za-z0-9_:.-]+", key):
            raise DomainError("Nieprawidłowy identyfikator operacji. Otwórz menu ponownie.")

    @staticmethod
    def _money(amount: int, *, signed=False):
        if type(amount) is not int or abs(amount) > 100_000_000 or (not signed and amount <= 0) or amount == 0:
            raise DomainError("Kwota musi wynosić od 0,01 zł do 1 000 000 zł.")

    @staticmethod
    def _log(s, admin_id, action, target, details):
        s.add(AdminAuditLog(admin_telegram_id=admin_id, action=action,
                            target=str(target), details=json.dumps(details, ensure_ascii=False)))

    @staticmethod
    def _ledger(s, user, amount, kind, reference):
        s.add(BalanceTransaction(user_id=user.id, amount_grosze=amount, kind=kind,
                                reference=reference, balance_after_grosze=(
                                    user.bonus_grosze if kind in {"referral_bonus", "referral_reversal"}
                                    else user.balance_grosze)))

    @staticmethod
    async def _user(s, telegram_id: int, *, active=False):
        user = await by_telegram_id(s, telegram_id)
        if user is None:
            raise DomainError("Najpierw utwórz konto poleceniem /start.")
        if active and user.status != "active":
            raise DomainError("Konto jest zablokowane. Skontaktuj się z pomocą.")
        return user

    async def register(self, telegram_id, username=None, first_name="", referrer_telegram_id=None):
        if type(telegram_id) is not int or telegram_id <= 0:
            raise DomainError("Nieprawidłowy identyfikator Telegram.")
        async with self._write() as s:
            user = await by_telegram_id(s, telegram_id)
            if user:
                user.username = username[:64] if username else None
                user.first_name = (first_name or "")[:128]
                return user
            referrer = None
            if referrer_telegram_id and referrer_telegram_id != telegram_id:
                referrer = await s.scalar(select(User).where(
                    User.telegram_id == referrer_telegram_id, User.status == "active"))
            user = User(telegram_id=telegram_id, username=username[:64] if username else None,
                        first_name=(first_name or "")[:128], referrer_id=referrer.id if referrer else None)
            s.add(user)
            await s.flush()
            if referrer:
                s.add(Referral(referred_id=user.id, referrer_id=referrer.id,
                               bonus_grosze=self.settings.referral_bonus_grosze, status="pending"))
            return user

    async def profile(self, telegram_id):
        async with self._read() as s:
            return await self._user(s, telegram_id)

    async def categories(self):
        async with self._read() as s:
            query = select(Category).where(Category.active.is_(True))
            if not self.settings.demo_mode:
                query = query.join(Product, Product.category_id == Category.id).where(
                    Product.active.is_(True), Product.is_example.is_(False), Product.stock > 0).distinct()
            return list(await s.scalars(query.order_by(Category.id)))

    async def products(self, category_id):
        async with self._read() as s:
            query = select(Product).join(Category).where(
                Product.category_id == category_id, Product.active.is_(True), Product.stock > 0,
                Category.active.is_(True))
            if not self.settings.demo_mode:
                query = query.where(Product.is_example.is_(False))
            return list(await s.scalars(query.order_by(Product.price_grosze, Product.id)))

    async def product(self, product_id):
        async with self._read() as s:
            product = await s.get(Product, product_id)
            if product is None:
                raise DomainError("Produkt nie istnieje.")
            return product

    async def purchase(self, telegram_id, product_id, request_key, expected_price_grosze=None):
        await self.ensure_mode()
        self._key(request_key)
        async with self._write() as s:
            user = await self._user(s, telegram_id, active=True)
            existing = await s.scalar(select(Order).where(Order.request_key == request_key))
            if existing:
                if existing.user_id != user.id or existing.product_id != product_id:
                    raise DomainError("Identyfikator operacji należy do innego zakupu.")
                return existing
            product = await s.get(Product, product_id)
            if product is None:
                raise DomainError("Produkt nie istnieje.")
            category = await s.get(Category, product.category_id)
            if not product.active or not category or not category.active or (not self.settings.demo_mode and product.is_example):
                raise DomainError("Produkt jest nieaktywny.")
            if product.stock <= 0:
                raise DomainError("Produkt jest chwilowo niedostępny.")
            if expected_price_grosze is not None and (type(expected_price_grosze) is not int or product.price_grosze != expected_price_grosze):
                raise DomainError("Cena produktu zmieniła się. Otwórz kartę produktu i potwierdź nową cenę.")
            if not self.settings.demo_mode:
                self._shipping_cipher()
            if user.balance_grosze < product.price_grosze:
                raise DomainError("Brak wystarczającego salda. Doładuj saldo przed zakupem.")
            order = Order(id=str(uuid4()), user_id=user.id, product_id=product.id,
                          product_name=f"{product.name} | {product.denomination} {product.currency} | {product.region}",
                          price_grosze=product.price_grosze, request_key=request_key, status="pending",
                          shipping_required=bool(not self.settings.demo_mode and product.requires_shipping),
                          fulfillment_due_at=(datetime.now(timezone.utc) + timedelta(days=7)) if not self.settings.demo_mode else None)
            s.add(order)
            user.balance_grosze -= product.price_grosze
            product.stock -= 1
            self._ledger(s, user, -product.price_grosze, "purchase", f"purchase:{order.id}")
            await s.flush()
            return order

    def _shipping_cipher(self):
        key = getattr(self.settings, "customer_data_key", "")
        if Fernet is None:
            raise DomainError("Brakuje pakietu cryptography. Zainstaluj zależności: pip install -r requirements.txt.")
        if not key:
            raise DomainError("Przed przyjmowaniem zamówień ustaw CUSTOMER_DATA_KEY w .env (python -m app.generate_customer_key).")
        try:
            return Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            raise DomainError("CUSTOMER_DATA_KEY jest nieprawidłowy. Wygeneruj nowy klucz poleceniem python -m app.generate_customer_key.") from None

    async def save_shipping(self, telegram_id, order_id, full_name, phone, email, locker_code):
        values = [str(value).strip() for value in (full_name, phone, email, locker_code)]
        if (not 2 <= len(values[0]) <= 120
                or not re.fullmatch(r"[+0-9() .-]{6,30}", values[1])
                or len(values[2]) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values[2])
                or not re.fullmatch(r"[A-Za-z0-9-]{5,8}", values[3])):
            raise DomainError("Sprawdź imię i nazwisko, telefon, e-mail oraz kod paczkomatu InPost (np. WAW01M).")
        cipher = self._shipping_cipher()
        async with self._write() as s:
            user = await self._user(s, telegram_id, active=True)
            order = await s.get(Order, order_id)
            if order is None or order.user_id != user.id:
                raise DomainError("Nie znaleziono Twojego zamówienia.")
            if not order.shipping_required or order.status not in {"pending", "processing"}:
                raise DomainError("Dane wysyłkowe nie są już wymagane dla tego zamówienia.")
            order.delivery_ciphertext = cipher.encrypt(json.dumps({"name": values[0], "phone": values[1], "email": values[2], "locker_code": values[3]}, ensure_ascii=False).encode()).decode()
            order.delivery_submitted_at = datetime.now(timezone.utc)
            await s.flush()
            return order

    async def shipping_details(self, admin_id, order_id):
        self._admin(admin_id)
        cipher = self._shipping_cipher()
        async with self._write() as s:
            order = await s.get(Order, order_id)
            if order is None or not order.delivery_ciphertext:
                return None
            try:
                details = json.loads(cipher.decrypt(order.delivery_ciphertext.encode()).decode())
            except (InvalidToken, ValueError, UnicodeDecodeError):
                raise DomainError("Nie można odszyfrować danych wysyłkowych; sprawdź CUSTOMER_DATA_KEY.") from None
            self._log(s, admin_id, "order.shipping.view", order.id, {"access": "authorized_admin"})
            return details

    async def orders(self, telegram_id):
        async with self._read() as s:
            user = await self._user(s, telegram_id)
            return list(await s.scalars(select(Order).where(Order.user_id == user.id).order_by(Order.created_at.desc())))

    async def transactions(self, telegram_id):
        async with self._read() as s:
            user = await self._user(s, telegram_id)
            return list(await s.scalars(select(BalanceTransaction).where(
                BalanceTransaction.user_id == user.id).order_by(BalanceTransaction.id.desc()).limit(100)))

    async def create_topup(self, telegram_id, amount_grosze, method, request_key, asset=None):
        topup = await self._create_topup_record(telegram_id, amount_grosze, method, request_key, asset)
        if not self.settings.demo_mode and self.settings.crypto_auto_rates and method == "crypto":
            return await self.auto_quote_topup(telegram_id, topup.id, suppress_rate_error=True)
        return topup

    async def _create_topup_record(self, telegram_id, amount_grosze, method, request_key, asset=None):
        self._key(request_key)
        self._money(amount_grosze)
        if not self.settings.demo_mode and not self.settings.admin_ids:
            raise DomainError("Wpłaty są niedostępne do czasu skonfigurowania administratora, który potwierdzi wpływ i uzna saldo.")
        if method not in {"blik", "crypto"}:
            raise DomainError("Wybierz metodę BLIK lub Crypto.")
        if asset is not None and asset not in CRYPTO_ASSETS:
            raise DomainError("Nieobsługiwana kryptowaluta. Wybierz ją z menu.")
        if method == "blik" and asset is not None:
            raise DomainError("Doładowanie BLIK nie może zawierać kryptowaluty.")
        async with self._write() as s:
            user = await self._user(s, telegram_id, active=True)
            existing = await s.scalar(select(DemoTopUp).where(DemoTopUp.request_key == request_key))
            if existing:
                if (existing.user_id, existing.amount_grosze, existing.method, existing.asset) != (user.id, amount_grosze, method, asset):
                    raise DomainError("Identyfikator operacji należy do innego zgłoszenia.")
                return existing
            topup_id = str(uuid4())
            if self.settings.demo_mode:
                intent = await self.payments.create_deposit(amount_grosze, method, topup_id)
                topup = DemoTopUp(id=topup_id, user_id=user.id, amount_grosze=amount_grosze,
                                 method=method, status="pending", external_id=intent.external_id,
                                 request_key=request_key, asset=asset)
            else:
                metadata, address = None, None
                if method == "blik":
                    if not self.settings.merchant_telegram_username:
                        raise DomainError("Kontakt BLIK nie został jeszcze ustawiony przez właściciela.")
                else:
                    if asset not in CRYPTO_ASSETS:
                        raise DomainError("Wybierz kryptowalutę i sieć z menu.")
                    metadata = CRYPTO_ASSETS[asset]
                    address = getattr(self.settings, metadata["setting"])
                    if not address:
                        raise DomainError("Adres tej kryptowaluty nie został jeszcze ustawiony przez właściciela.")
                    try:
                        validate_address(asset, address)
                    except ValueError as exc:
                        raise DomainError(str(exc)) from None
                topup = DemoTopUp(id=topup_id, user_id=user.id, amount_grosze=amount_grosze,
                                 method=method, status="awaiting_quote" if method == "crypto" else "awaiting_payment",
                                 external_id=f"MANUAL-{topup_id}", request_key=request_key,
                                 asset=asset, network=metadata["network"] if metadata else None, wallet_address=address)
            s.add(topup)
            await s.flush()
            return topup

    async def auto_quote_topup(self, telegram_id, topup_id, *, suppress_rate_error=False):
        """Read a rate without a DB lock, then atomically freeze the quote.

        Existing valid quotes remain unchanged across refreshes, duplicate
        callbacks and restarts. The request persists if the provider is down.
        """
        if self.settings.demo_mode or not self.settings.crypto_auto_rates:
            raise DomainError("Automatyczne wyceny nie są włączone w tym trybie.")
        async with self._read() as s:
            user = await self._user(s, telegram_id, active=True)
            topup = await s.get(DemoTopUp, topup_id)
            if topup is None or topup.user_id != user.id or topup.method != "crypto":
                raise DomainError("Zgłoszenie wpłaty kryptowalutowej nie istnieje.")
            if topup.status not in {"awaiting_quote", "awaiting_payment"}:
                return topup
            now = datetime.now(timezone.utc)
            if topup.crypto_amount and topup.quote_expires_at and self._utc(topup.quote_expires_at) > now:
                return topup
            asset = topup.asset
        try:
            snapshot = await self.rate_cache.get(asset)
        except RateUnavailable as exc:
            if suppress_rate_error:
                return topup
            raise DomainError(str(exc)) from None
        # Fetch above has completed; only the DB state transition is locked.
        async with self._write() as s:
            user = await self._user(s, telegram_id, active=True)
            current = await s.get(DemoTopUp, topup_id)
            if current is None or current.user_id != user.id or current.method != "crypto":
                raise DomainError("Zgłoszenie wpłaty kryptowalutowej nie istnieje.")
            now = datetime.now(timezone.utc)
            if current.status not in {"awaiting_quote", "awaiting_payment"}:
                return current
            if current.crypto_amount and current.quote_expires_at and self._utc(current.quote_expires_at) > now:
                return current
            fresh_until = min(self._utc(snapshot.fetched_at), self._utc(snapshot.source_updated_at)) + timedelta(seconds=self.settings.rate_max_age_seconds)
            if fresh_until <= now:
                if suppress_rate_error:
                    return current
                raise DomainError("Kurs wygasł. Spróbuj odświeżyć wycenę później.")
            price = select_price(snapshot, self.settings.rate_price_mode)
            current.crypto_amount = crypto_for_pln(current.amount_grosze, price, current.asset)
            current.quote_expires_at = min(now + timedelta(minutes=30), fresh_until)
            current.status = "awaiting_payment"
            current.rate_pln = str(price)
            current.rate_source = snapshot.source
            current.rate_source_updated_at = snapshot.source_updated_at
            current.rate_fetched_at = snapshot.fetched_at
            current.rate_mode = self.settings.rate_price_mode
            # Automatic system quotes have no administrator actor and do not
            # alter a balance. The immutable rate evidence is on the request.
            await s.flush()
            return current

    async def admin_quote_topup(self, admin_id, topup_id, crypto_amount):
        self._admin(admin_id)
        if self.settings.crypto_auto_rates:
            raise DomainError("Kwotę kryptowaluty oblicza automatycznie bot. Ręczna zmiana kursu jest wyłączona.")
        raw = crypto_amount
        if self.settings.demo_mode:
            raise DomainError("Wyceny kryptowalut są dostępne wyłącznie w trybie wpłat ręcznych.")
        async with self._write() as s:
            topup = await s.get(DemoTopUp, topup_id)
            if topup is None or topup.method != "crypto":
                raise DomainError("Zgłoszenie wpłaty kryptowalutowej nie istnieje.")
            if topup.status not in {"awaiting_quote", "awaiting_payment"}:
                raise DomainError("Nie można zmienić wyceny zgłoszonej lub rozpatrzonej wpłaty.")
            try:
                value = normalize_crypto_amount(topup.asset, raw)
            except ValueError as exc:
                raise DomainError(str(exc)) from None
            if topup.quote_expires_at is not None and self._utc(topup.quote_expires_at) > datetime.now(timezone.utc):
                from decimal import Decimal
                if topup.crypto_amount is not None and Decimal(value) == Decimal(topup.crypto_amount):
                    return topup
                raise DomainError("Ta wycena nadal obowiązuje. Nie można zmienić kwoty przed jej wygaśnięciem, ponieważ użytkownik mógł już wykonać przelew.")
            topup.crypto_amount = value
            topup.quote_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
            topup.status = "awaiting_payment"
            self._log(s, admin_id, "topup.quote", topup.id, {
                "amount_pln_grosze": topup.amount_grosze, "asset": topup.asset,
                "crypto_amount": value, "expires_at": topup.quote_expires_at.isoformat(),
            })
            await s.flush()
            return topup

    async def submit_topup(self, telegram_id, topup_id, quote_token=None):
        if self.settings.demo_mode:
            raise DomainError("W DEMO zgłoszenie zatwierdza administrator bez rzeczywistego przelewu.")
        async with self._write() as s:
            user = await self._user(s, telegram_id, active=True)
            topup = await s.get(DemoTopUp, topup_id)
            if topup is None or topup.user_id != user.id:
                raise DomainError("Zgłoszenie doładowania nie istnieje.")
            if topup.method == "crypto" and (not isinstance(quote_token, str) or not quote_token or not hmac.compare_digest(quote_token, self.quote_token(topup))):
                raise DomainError("Ten przycisk dotyczy innej lub nieaktualnej wyceny. Odśwież status zgłoszenia i sprawdź kwotę przed wpłatą.")
            if topup.status in {"submitted", "approved"}:
                return topup
            if topup.status != "awaiting_payment":
                raise DomainError("Zgłoszenie nie oczekuje na potwierdzenie wpłaty. Sprawdź status i wycenę.")
            if topup.method == "crypto" and (
                topup.crypto_amount is None or topup.quote_expires_at is None
                or self._utc(topup.quote_expires_at) <= datetime.now(timezone.utc)
            ):
                raise DomainError("Wycena wygasła. Poproś administratora o nową przed wykonaniem przelewu. Jeśli środki już wysłano, skontaktuj się z nim w celu ręcznego rozliczenia.")
            topup.status = "submitted"
            topup.submitted_at = datetime.now(timezone.utc)
            await s.flush()
            return topup

    async def topups(self, telegram_id):
        async with self._read() as s:
            user = await self._user(s, telegram_id)
            return list(await s.scalars(select(DemoTopUp).where(
                DemoTopUp.user_id == user.id).order_by(DemoTopUp.created_at.desc()).limit(100)))

    async def topup(self, telegram_id, topup_id):
        async with self._read() as s:
            user = await self._user(s, telegram_id)
            topup = await s.get(DemoTopUp, topup_id)
            if topup is None or topup.user_id != user.id:
                raise DomainError("Zgłoszenie doładowania nie istnieje.")
            return topup

    async def referral_stats(self, telegram_id):
        async with self._read() as s:
            user = await self._user(s, telegram_id)
            invited = await s.scalar(select(func.count(Referral.id)).where(Referral.referrer_id == user.id))
            return {"invited": invited, "successful": user.successful_referrals,
                    "earned_grosze": user.bonus_grosze}

    async def admin_user(self, admin_id, target_telegram_id):
        self._admin(admin_id)
        return await self.profile(target_telegram_id)

    async def telegram_for_user(self, user_id):
        """Resolve an internal ID for transport notifications, never from callback input."""
        async with self._read() as s:
            user = await s.get(User, user_id)
            if user is None:
                raise DomainError("Użytkownik nie istnieje.")
            return user.telegram_id

    async def admin_products(self, admin_id):
        self._admin(admin_id)
        async with self._read() as s:
            return list(await s.scalars(select(Product).order_by(Product.id)))

    async def admin_categories(self, admin_id):
        self._admin(admin_id)
        async with self._read() as s:
            return list(await s.scalars(select(Category).order_by(Category.id)))

    async def admin_orders(self, admin_id):
        self._admin(admin_id)
        async with self._read() as s:
            return list(await s.scalars(select(Order).order_by(Order.created_at.desc())))

    async def admin_topups(self, admin_id):
        self._admin(admin_id)
        async with self._read() as s:
            return list(await s.scalars(select(DemoTopUp).order_by(DemoTopUp.created_at.desc())))

    async def admin_stats(self, admin_id):
        self._admin(admin_id)
        async with self._read() as s:
            return {
                "users": await s.scalar(select(func.count(User.id))),
                "orders": await s.scalar(select(func.count(Order.id))),
                "completed": await s.scalar(select(func.count(Order.id)).where(Order.status == "completed")),
                "turnover_grosze": await s.scalar(select(func.coalesce(func.sum(Order.price_grosze), 0)).where(Order.status == "completed")),
                "referrals": await s.scalar(select(func.count(Referral.id)).where(Referral.status == "rewarded")),
                "pending_topups": await s.scalar(select(func.count(DemoTopUp.id)).where(DemoTopUp.status.in_(["pending", "awaiting_quote", "awaiting_payment", "submitted"]))),
                "pending_orders": await s.scalar(select(func.count(Order.id)).where(Order.status.in_(["pending", "processing"]))),
            }

    async def admin_order(self, admin_id, order_id, action):
        self._admin(admin_id)
        await self.ensure_mode()
        if action not in {"processing", "completed", "cancelled", "refunded"}:
            raise DomainError("Nieprawidłowa operacja na zamówieniu.")
        async with self._write() as s:
            order = await s.get(Order, order_id)
            if order is None:
                raise DomainError("Zamówienie nie istnieje.")
            if order.status == action:
                return order
            allowed = {"pending": {"processing", "completed", "cancelled"},
                       "processing": {"completed", "cancelled"},
                       "completed": {"refunded"}, "cancelled": set(), "refunded": set()}
            if action not in allowed.get(order.status, set()):
                raise DomainError("Ta zmiana statusu nie jest możliwa. Odśwież zamówienie.")
            previous = order.status
            user = await s.get(User, order.user_id)
            if action == "completed":
                if order.shipping_required and not order.delivery_ciphertext:
                    raise DomainError("Nie można zakończyć zamówienia: użytkownik nie podał danych wysyłkowych.")
                if self.settings.demo_mode:
                    order.demo_code = "DEMO-NOT-VALID-" + uuid4().hex.upper()
                user.purchase_count += 1
                user.total_spent_grosze += order.price_grosze
                referral = await s.scalar(select(Referral).where(Referral.referred_id == user.id)) if self.settings.demo_mode else None
                if referral and referral.status == "pending":
                    referrer = await s.get(User, referral.referrer_id)
                    referrer.bonus_grosze += referral.bonus_grosze
                    referrer.successful_referrals += 1
                    referral.status = "rewarded"
                    referral.qualifying_order_id = order.id
                    # Bonus is tracked separately, never spendable or withdrawn.
                    self._ledger(s, referrer, referral.bonus_grosze, "referral_bonus", f"referral:{referral.id}")
            elif action in {"cancelled", "refunded"}:
                user.balance_grosze += order.price_grosze
                product = await s.get(Product, order.product_id)
                if product:
                    product.stock += 1
                self._ledger(s, user, order.price_grosze, "refund", f"refund:{order.id}")
                if previous == "completed":
                    user.purchase_count = max(0, user.purchase_count - 1)
                    user.total_spent_grosze -= order.price_grosze
                    referral = await s.scalar(select(Referral).where(
                        Referral.qualifying_order_id == order.id, Referral.status == "rewarded")) if self.settings.demo_mode else None
                    if referral:
                        referrer = await s.get(User, referral.referrer_id)
                        referrer.bonus_grosze -= referral.bonus_grosze
                        referrer.successful_referrals -= 1
                        referral.status = "reversed"
                        self._ledger(s, referrer, -referral.bonus_grosze, "referral_reversal", f"referral-reversal:{referral.id}")
                order.demo_code = None
            if action in {"completed", "cancelled", "refunded"}:
                # Shipping data is needed only while the order is being fulfilled.
                # Erase it as soon as the order is final; never copy it to audit logs.
                order.delivery_ciphertext = None
                order.delivery_submitted_at = None
            order.status = action
            self._log(s, admin_id, f"order.{action}", order.id, {"previous": previous})
            await s.flush()
            return order

    async def admin_topup(self, admin_id, topup_id, approve, confirmation_reference=None):
        self._admin(admin_id)
        if type(approve) is not bool:
            raise DomainError("Nieprawidłowa decyzja.")
        async with self._write() as s:
            topup = await s.get(DemoTopUp, topup_id)
            if topup is None:
                raise DomainError("Zgłoszenie nie istnieje.")
            desired = "approved" if approve else "rejected"
            reference = None
            if approve and not self.settings.demo_mode:
                reference = self._confirmation_reference(topup, confirmation_reference)
            if topup.status == desired:
                if reference is not None and topup.confirmation_reference != reference:
                    raise DomainError("Zgłoszenie zatwierdzono z innym potwierdzeniem. Nie dopisano środków ponownie.")
                return topup
            if self.settings.demo_mode:
                if topup.status != "pending":
                    raise DomainError("Zgłoszenie zostało już rozpatrzone.")
            elif topup.status not in {"awaiting_quote", "awaiting_payment", "submitted"}:
                raise DomainError("Zgłoszenie zostało już rozpatrzone.")
            elif approve:
                if topup.status != "submitted":
                    raise DomainError("Najpierw użytkownik musi zgłosić wykonanie wpłaty. Samo utworzenie zgłoszenia nie potwierdza przelewu.")
                if await s.scalar(select(DemoTopUp.id).where(DemoTopUp.confirmation_reference == reference)):
                    raise DomainError("To potwierdzenie wpłaty zostało już wykorzystane. Nie dopisano środków.")
                if topup.method == "crypto" and (topup.crypto_amount is None or topup.submitted_at is None):
                    raise DomainError("Brak kompletnej wyceny lub zgłoszenia wpłaty.")
                topup.confirmation_reference = reference
            if approve:
                user = await s.get(User, topup.user_id)
                user.balance_grosze += topup.amount_grosze
                self._ledger(s, user, topup.amount_grosze,
                             "demo_topup" if self.settings.demo_mode else "manual_topup", f"topup:{topup.id}")
            topup.status = desired
            self._log(s, admin_id, f"topup.{desired}", topup.id, {
                "amount_grosze": topup.amount_grosze, "balance_mode": self.balance_mode,
                "confirmation_reference": topup.confirmation_reference,
            })
            await s.flush()
            return topup

    async def admin_balance(self, admin_id, target_telegram_id, delta_grosze, request_key):
        self._admin(admin_id)
        self._key(request_key)
        self._money(delta_grosze, signed=True)
        async with self._write() as s:
            user = await self._user(s, target_telegram_id)
            reference = f"admin:{admin_id}:{request_key}"
            existing = await s.scalar(select(BalanceTransaction).where(BalanceTransaction.reference == reference))
            if existing:
                if existing.user_id != user.id or existing.amount_grosze != delta_grosze:
                    raise DomainError("Identyfikator operacji należy do innej korekty.")
                return user
            if user.balance_grosze + delta_grosze < 0:
                raise DomainError("Korekta spowodowałaby ujemne saldo.")
            user.balance_grosze += delta_grosze
            self._ledger(s, user, delta_grosze, "admin_adjustment", reference)
            self._log(s, admin_id, "balance.adjust", user.telegram_id,
                      {"delta_grosze": delta_grosze, "request_key": request_key})
            await s.flush()
            return user

    async def admin_category(self, admin_id, name):
        self._admin(admin_id)
        name = str(name).strip()
        if not 1 <= len(name) <= 80:
            raise DomainError("Nazwa kategorii musi mieć 1–80 znaków.")
        async with self._write() as s:
            category = await s.scalar(select(Category).where(func.lower(Category.name) == name.lower()))
            if category:
                return category
            category = Category(name=name, active=True)
            s.add(category)
            await s.flush()
            self._log(s, admin_id, "category.create", category.id, {"name": name})
            return category

    async def admin_product(self, admin_id, product_id=None, **fields):
        self._admin(admin_id)
        allowed = {"name", "category_id", "denomination", "currency", "region", "price_grosze", "description", "stock", "active", "is_example", "requires_shipping"}
        if not fields or set(fields) - allowed:
            raise DomainError("Nieprawidłowe pola produktu.")
        if product_id is None and not {"name", "category_id", "denomination", "currency", "region", "price_grosze", "description", "stock"} <= fields.keys():
            raise DomainError("Uzupełnij wszystkie dane nowego produktu.")
        limits = {"name": 120, "denomination": 40, "currency": 8, "region": 80, "description": 1500}
        for key, limit in limits.items():
            if key in fields:
                if not isinstance(fields[key], str) or len(fields[key].strip()) > limit or (key != "description" and not fields[key].strip()):
                    raise DomainError(f"Nieprawidłowa wartość pola {key}.")
                fields[key] = fields[key].strip()
        if "currency" in fields:
            fields["currency"] = fields["currency"].upper()
            if not re.fullmatch(r"[A-Z]{3}", fields["currency"]):
                raise DomainError("Waluta musi być kodem 3-literowym, np. USD.")
        if "category_id" in fields and (type(fields["category_id"]) is not int or fields["category_id"] <= 0):
            raise DomainError("Nieprawidłowy identyfikator kategorii.")
        if "price_grosze" in fields:
            self._money(fields["price_grosze"])
        if "stock" in fields and (type(fields["stock"]) is not int or not 0 <= fields["stock"] <= 1_000_000):
            raise DomainError("Dostępność musi być liczbą od 0 do 1 000 000.")
        for flag in ("active", "is_example", "requires_shipping"):
            if flag in fields and type(fields[flag]) is not bool:
                raise DomainError(f"Nieprawidłowa wartość pola {flag}.")
        if "active" in fields and type(fields["active"]) is not bool:
            raise DomainError("Status aktywności musi być wartością logiczną.")
        async with self._write() as s:
            if "category_id" in fields and await s.get(Category, fields["category_id"]) is None:
                raise DomainError("Kategoria nie istnieje.")
            if product_id is None:
                product = Product(**fields)
                s.add(product)
                action = "product.create"
            else:
                product = await s.get(Product, product_id)
                if product is None:
                    raise DomainError("Produkt nie istnieje.")
                for key, value in fields.items():
                    setattr(product, key, value)
                action = "product.update"
            await s.flush()
            self._log(s, admin_id, action, product.id, fields)
            return product

    async def admin_delete_product(self, admin_id, product_id):
        self._admin(admin_id)
        async with self._write() as s:
            product = await s.get(Product, product_id)
            if product is None:
                raise DomainError("Produkt nie istnieje.")
            if await s.scalar(select(Order.id).where(Order.product_id == product_id).limit(1)):
                raise DomainError("Produkt ma historię zamówień. Możesz go ukryć zamiast usuwać.")
            self._log(s, admin_id, "product.delete", product.id, {"name": product.name})
            await s.delete(product)

    async def admin_set_user_status(self, admin_id, target_telegram_id, status):
        self._admin(admin_id)
        if status not in {"active", "blocked"}:
            raise DomainError("Nieprawidłowy status konta.")
        async with self._write() as s:
            user = await self._user(s, target_telegram_id)
            previous = user.status
            user.status = status
            self._log(s, admin_id, "user.status", user.telegram_id, {"previous": previous, "status": status})
            return user

    async def admin_setting(self, admin_id, key, value):
        self._admin(admin_id)
        if key != "support_text" or not isinstance(value, str) or not 1 <= len(value) <= 2000:
            raise DomainError("Możesz zmienić wyłącznie tekst pomocy (1–2000 znaków).")
        async with self._write() as s:
            setting = await s.get(BotSettings, key)
            if setting:
                setting.value = value
            else:
                s.add(BotSettings(key=key, value=value))
            self._log(s, admin_id, "setting.update", key, {"value": value})

    async def setting(self, key, default=""):
        async with self._read() as s:
            setting = await s.get(BotSettings, key)
            return setting.value if setting else default

    async def broadcast_recipients(self, admin_id):
        self._admin(admin_id)
        async with self._read() as s:
            return list(await s.scalars(select(User.telegram_id).where(User.status == "active")))

    async def audit(self, admin_id, action, target, details):
        self._admin(admin_id)
        async with self._write() as s:
            self._log(s, admin_id, action[:80], target, details)

    async def seed(self):
        """Explicit fictional inventory, not a promise of real-region availability."""
        examples = {
            "Steam": [("5", "USD", "DEMO US", 2200), ("10", "USD", "DEMO US", 4400), ("20", "USD", "DEMO US", 8800), ("50", "USD", "DEMO US", 22000)],
            "PlayStation": [("50", "PLN", "DEMO PL", 5000), ("100", "PLN", "DEMO PL", 10000)],
            "Xbox": [("25", "PLN", "DEMO PL", 2500), ("50", "PLN", "DEMO PL", 5000)],
            "Roblox": [("10", "USD", "DEMO US", 4400)],
            "Google Play": [("25", "PLN", "DEMO PL", 2500), ("50", "PLN", "DEMO PL", 5000)],
            "Apple Gift Card": [("10", "USD", "DEMO US", 4400)],
        }
        async with self._write() as s:
            if not self.settings.demo_mode:
                marker = await s.get(BotSettings, "real_sample_products_v1")
                if marker:
                    return
                category = Category(name="Przykłady — skonfiguruj przed sprzedażą", active=True)
                s.add(category)
                await s.flush()
                for name, denomination, currency, region, price in (("Produkt przykładowy 1", "10", "USD", "Ustaw region", 5000), ("Produkt przykładowy 2", "25", "PLN", "Polska", 10000)):
                    s.add(Product(name=name, category_id=category.id, denomination=denomination,
                                  currency=currency, region=region, price_grosze=price,
                                  description="Przykładowy wpis. Edytuj cenę, region, dostępność i opis; aktywuj dopiero po zapewnieniu prawdziwego produktu.",
                                  stock=0, active=False, is_example=True, requires_shipping=True))
                s.add(BotSettings(key="real_sample_products_v1", value="1"))
                return
            marker = await s.get(BotSettings, "seed_version")
            if marker:
                return
            for name, entries in examples.items():
                category = await s.scalar(select(Category).where(Category.name == name))
                if category is None:
                    category = Category(name=name, active=True)
                    s.add(category)
                    await s.flush()
                for denomination, currency, region, price in entries:
                    s.add(Product(name=f"{name} DEMO", category_id=category.id,
                                  denomination=denomination, currency=currency, region=region,
                                  price_grosze=price, description="Fikcyjny produkt szkoleniowy. Nie jest kartą podarunkową ani kodem do realizacji.",
                                  stock=100, active=True))
            s.add(BotSettings(key="seed_version", value="1"))
