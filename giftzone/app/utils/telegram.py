import logging
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup

log = logging.getLogger(__name__)


async def render(event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(event, CallbackQuery):
        if event.message is None or not isinstance(event.message, Message):
            await event.answer('Otwórz ponownie menu przez /start.', show_alert=True)
            return
        try:
            if event.message.photo and len(text) <= 1024:
                await event.message.edit_caption(caption=text, reply_markup=markup)
            elif event.message.photo:
                await event.message.delete()
                await event.message.answer(text, reply_markup=markup)
            else:
                await event.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if 'message is not modified' not in str(exc):
                await event.message.answer(text, reply_markup=markup)
    else:
        await event.answer(text, reply_markup=markup)


async def notify(bot, recipient: int, text: str, markup=None) -> bool:
    try:
        await bot.send_message(recipient, text, reply_markup=markup)
        return True
    except TelegramAPIError as exc:
        log.warning('Telegram notification failed: %s', type(exc).__name__)
        return False
