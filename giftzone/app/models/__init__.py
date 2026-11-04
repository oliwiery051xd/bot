from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc)


def uuidstr():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("balance_grosze >= 0"), CheckConstraint("bonus_grosze >= 0"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    balance_grosze: Mapped[int] = mapped_column(Integer, default=0)
    bonus_grosze: Mapped[int] = mapped_column(Integer, default=0)
    purchase_count: Mapped[int] = mapped_column(Integer, default=0)
    total_spent_grosze: Mapped[int] = mapped_column(Integer, default=0)
    successful_referrals: Mapped[int] = mapped_column(Integer, default=0)
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="active")


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (CheckConstraint("price_grosze > 0"), CheckConstraint("stock >= 0"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), index=True)
    denomination: Mapped[str] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(10))
    region: Mapped[str] = mapped_column(String(100))
    price_grosze: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")
    stock: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_example: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_shipping: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (CheckConstraint("price_grosze > 0"), Index("ix_orders_status_created", "status", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuidstr)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    product_name: Mapped[str] = mapped_column(String(400))
    price_grosze: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    demo_code: Mapped[str | None] = mapped_column(String(100))
    request_key: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    shipping_required: Mapped[bool] = mapped_column(Boolean, default=False)
    delivery_ciphertext: Mapped[str | None] = mapped_column(Text)
    delivery_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfillment_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BalanceTransaction(Base):
    __tablename__ = "balance_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount_grosze: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    reference: Mapped[str] = mapped_column(String(150), unique=True)
    balance_after_grosze: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DemoTopUp(Base):
    __tablename__ = "demo_topups"
    __table_args__ = (CheckConstraint("amount_grosze > 0"), Index("ix_topups_status_created", "status", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuidstr)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount_grosze: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    external_id: Mapped[str] = mapped_column(String(100), unique=True)
    request_key: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    asset: Mapped[str | None] = mapped_column(String(10))
    network: Mapped[str | None] = mapped_column(String(80))
    wallet_address: Mapped[str | None] = mapped_column(String(200))
    crypto_amount: Mapped[str | None] = mapped_column(String(100))
    quote_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_reference: Mapped[str | None] = mapped_column(String(180), unique=True)
    rate_pln: Mapped[str | None] = mapped_column(String(100))
    rate_source: Mapped[str | None] = mapped_column(String(100))
    rate_source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rate_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rate_mode: Mapped[str | None] = mapped_column(String(20))


class Referral(Base):
    __tablename__ = "referrals"
    id: Mapped[int] = mapped_column(primary_key=True)
    referred_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    qualifying_order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), unique=True)
    bonus_grosze: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(150))
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BotSettings(Base):
    __tablename__ = "bot_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
