import json
import re
from typing import Annotated, Literal

from pydantic import Field, StrictInt, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(default="", repr=False)
    customer_data_key: str = Field(default="", repr=False)
    admin_ids: Annotated[list[StrictInt], NoDecode] = Field(default_factory=list)
    database_url: str = Field(default="sqlite+aiosqlite:///./monstal.db", repr=False)
    demo_mode: bool = False
    merchant_telegram_username: str = ""
    crypto_btc_address: str = ""
    crypto_eth_address: str = ""
    crypto_usdc_address: str = ""
    crypto_sol_address: str = ""
    crypto_ltc_address: str = ""
    crypto_xmr_address: str = ""
    crypto_auto_rates: bool = True
    rate_price_mode: Literal["current", "low_24h"] = "current"
    rate_refresh_seconds: int = Field(default=300, ge=60, le=86400)
    rate_max_age_seconds: int = Field(default=86400, ge=300, le=86400)
    coingecko_api_key: str = Field(default="", repr=False)
    referral_bonus_grosze: int = Field(default=1000, ge=0, le=1_000_000)
    log_level: str = "INFO"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return json.loads(value) if value.startswith("[") else [int(x.strip()) for x in value.split(",") if x.strip()]
        return value

    @field_validator("admin_ids")
    @classmethod
    def validate_admin_ids(cls, value):
        if any(not 0 < item < 2**63 for item in value):
            raise ValueError("ADMIN_IDS: dozwolone są wyłącznie dodatnie identyfikatory kont Telegram.")
        return list(dict.fromkeys(value))

    @field_validator("merchant_telegram_username", mode="before")
    @classmethod
    def merchant_username(cls, value):
        value = str(value or "").strip().removeprefix("@")
        if value and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
            raise ValueError("MERCHANT_TELEGRAM_USERNAME musi zawierać prawidłowy nick Telegram (5–32 znaki).")
        return value

    @field_validator("crypto_btc_address", "crypto_eth_address", "crypto_usdc_address", "crypto_sol_address", "crypto_ltc_address", "crypto_xmr_address", mode="before")
    @classmethod
    def address_format(cls, value, info):
        from app.services.manual_payments import validate_address
        value = str(value or "").strip()
        if value:
            code = info.field_name.split("_")[1].upper()
            validate_address(code, value)
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value):
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Nieprawidłowy LOG_LEVEL")
        return value

    def is_admin(self, telegram_id: int) -> bool:
        return type(telegram_id) is int and telegram_id > 0 and telegram_id in self.admin_ids
