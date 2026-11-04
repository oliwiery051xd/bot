import logging
from html import escape
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message
from sqlalchemy.exc import SQLAlchemyError
from app.services.shop import DomainError

log = logging.getLogger(__name__)


class UserFacingErrors(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except (DomainError, ValueError) as exc:
            text = str(exc)
        except SQLAlchemyError as exc:
            log.error('Database operation failed: %s', type(exc).__name__)
            text = 'Baza danych jest chwilowo niedostępna. Spróbuj ponownie. Operacje są chronione przed podwójnym księgowaniem.'
        except TelegramAPIError as exc:
            log.warning('Telegram request failed: %s', type(exc).__name__)
            return
        except Exception as exc:
            log.error('Unhandled handler error: %s', type(exc).__name__)
            text = 'Wystąpił błąd. Otwórz /start i spróbuj ponownie.'
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(text[:190], show_alert=True)
            elif isinstance(event, Message):
                await event.answer(escape(text))
        except TelegramAPIError:
            pass
