"""Run with python -m app.main from giftzone/."""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.memory import SimpleEventIsolation
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.database import Database
from app.handlers import create_router
from app.services.shop import DomainError, ShopService
from app.utils.session import TelegramSession

logger = logging.getLogger("monstal")


async def run() -> int:
    try:
        settings = Settings()
    except (ValidationError, ValueError):
        print("Nieprawidłowa konfiguracja .env. Sprawdź ADMIN_IDS, DATABASE_URL, DEMO_MODE i dane metod wpłat.", file=sys.stderr)
        return 2
    if not settings.bot_token.strip():
        print("Brak BOT_TOKEN. Skopiuj .env.example do .env i wpisz token z @BotFather w BOT_TOKEN. "
              "Następnie wykonaj: alembic upgrade head; python -m app.seed; python -m app.main", file=sys.stderr)
        return 2
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Do not let third-party debug logs include request URLs, headers, or tokens.
    for name in ("aiohttp", "aiogram", "sqlalchemy", "httpx", "httpcore", "aiosqlite"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        bot = Bot(settings.bot_token, session=TelegramSession(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    except (ValueError, TypeError):
        print("BOT_TOKEN ma nieprawidłowy format. Ustaw token uzyskany z @BotFather w pliku .env.", file=sys.stderr)
        return 2
    db = Database(settings.database_url)
    rate_stop = asyncio.Event()
    rate_task = None
    try:
        async with db.sessions() as session:
            await session.execute(text("SELECT id FROM users LIMIT 1"))
        service = ShopService(db, settings)
        await service.ensure_mode()
        dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        dp.include_router(create_router(service, settings))
        if not settings.demo_mode and settings.crypto_auto_rates and any(option['configured'] for option in service.crypto_options()):
            rate_task = asyncio.create_task(service.rate_cache.run(rate_stop), name='monstal-rates')
        # Polling is the implemented transport. HTTPS webhook server, signature
        # handling and real payment providers are intentionally not implemented.
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_name(name="MONSTAL")
        logger.info("MONSTAL uruchomiony: %s (long polling).", "DEMO" if settings.demo_mode else "ręcznie weryfikowane wpłaty PLN")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False)
        return 0
    except DomainError as exc:
        logger.error("%s", str(exc))
        return 1
    except SQLAlchemyError:
        logger.error("Błąd bazy danych. Sprawdź DATABASE_URL i wykonaj alembic upgrade head.")
        return 1
    except TelegramAPIError as exc:
        logger.error("Nie udało się połączyć z Telegramem (%s). Sprawdź token i dostęp sieciowy.", type(exc).__name__)
        return 1
    finally:
        rate_stop.set()
        if rate_task is not None:
            rate_task.cancel()
            try:
                await rate_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error("Zadanie aktualizacji kursów zakończone (%s).", type(exc).__name__)
        await bot.session.close()
        await db.dispose()


def main():
    try:
        code = asyncio.run(run())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
