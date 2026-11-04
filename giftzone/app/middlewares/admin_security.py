"""Administrative access is bound to an allowlisted human in a private chat.

This outer middleware runs before admin command, FSM and callback handlers.
Service-layer authorization and one-use confirmation tokens remain independent
checks; a username or forwarded panel never grants administrative access.
"""
import logging
import re

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

log = logging.getLogger(__name__)
ADMIN_COMMAND = re.compile(r"^\s*/admin(?:@[A-Za-z0-9_]+)?(?:\s|$)", re.IGNORECASE)


class AdminSecurity(BaseMiddleware):
    def __init__(self, settings):
        self.settings = settings

    async def __call__(self, handler, event, data):
        state_name = data.get("raw_state") or ""
        is_admin_state = state_name.startswith("AdminInput:")
        protected = (
            isinstance(event, CallbackQuery) and (event.data or "").startswith("a:")
        ) or (
            isinstance(event, Message) and ADMIN_COMMAND.match(event.text or event.caption or "")
        ) or is_admin_state
        if not protected:
            return await handler(event, data)

        actor = getattr(event, "from_user", None)
        message = event.message if isinstance(event, CallbackQuery) else event
        chat = getattr(message, "chat", None)
        reason = None
        if actor is None or actor.is_bot or type(actor.id) is not int or actor.id <= 0:
            reason = "invalid_actor"
        elif not self.settings.is_admin(actor.id):
            reason = "not_allowlisted"
        elif not isinstance(message, Message):
            reason = "inaccessible_message"
        elif chat is None or chat.type != ChatType.PRIVATE or chat.id != actor.id:
            reason = "not_direct_private_chat"
        elif getattr(message, "sender_chat", None) is not None or getattr(message, "business_connection_id", None):
            reason = "indirect_sender"
        elif getattr(message, "forward_origin", None) is not None:
            reason = "forwarded_message"

        if reason is None:
            return await handler(event, data)

        # Do not log callback data, form values, usernames, receipts or tokens.
        log.warning("Administrative access denied (%s).", reason)
        if is_admin_state and data.get("state") is not None:
            await data["state"].clear()
        text = (
            "Panel administratora jest dostępny wyłącznie w prywatnej rozmowie z botem. Otwórz tam /admin."
            if actor is not None and not actor.is_bot and self.settings.is_admin(actor.id)
            else "Brak uprawnień administratora. Dostęp wymaga Telegram ID z ADMIN_IDS."
        )
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            elif isinstance(event, Message) and chat is not None:
                await event.answer(text)
        except TelegramAPIError:
            # Denial remains effective even if Telegram cannot deliver the notice.
            log.info("Administrative access-denial notice could not be delivered.")
        return None
