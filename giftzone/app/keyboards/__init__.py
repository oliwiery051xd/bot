from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def keyboard(rows: list[list[tuple[str, str]]], *, back: str | None = 'menu') -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows]
    if back is not None:
        buttons.append([InlineKeyboardButton(text='🔙 Powrót do menu głównego', callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def main_menu() -> InlineKeyboardMarkup:
    return keyboard([
        [('👤 Mój profil', 'profile'), ('🎁 Gift Cards', 'catalog')],
        [('💳 Doładuj saldo', 'topups'), ('🤝 Polecenia', 'referrals')],
        [('📦 Moje zamówienia', 'orders:0'), ('🛟 Pomoc', 'help')],
    ], back=None)
