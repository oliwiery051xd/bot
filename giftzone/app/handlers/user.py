from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, Message

from app.keyboards import keyboard, main_menu
from app.utils.formatting import amount, money, safe, title
from app.utils.telegram import notify, render

WELCOME = title('🎁 MONSTAL') + (
    'Witaj w MONSTAL!\n\n💎 Twoje centrum cyfrowych kart podarunkowych.\n\n'
    '🛍️ Przeglądaj dostępne produkty.\n💳 Zarządzaj testowym saldem.\n'
    '🎁 Zdobywaj bonusy za polecenia.\n📦 Śledź swoje zamówienia.\n\n'
    '<b>Wersja: DEMO</b>\nProdukty, saldo i kody są fikcyjne. Nie wysyłaj pieniędzy.'
)
REAL_WELCOME = title('🎁 MONSTAL') + (
    'Witaj w MONSTAL!\n\n💳 Twoje saldo w PLN.\n'
    '🟣 BLIK — ustalenia bezpośrednio z właścicielem.\n'
    '🪙 Crypto — wybierz walutę i sieć.\n'
    '👤 Sprawdź profil i historię wpłat.\n\n'
    'Wpłaty sprawdza i księguje ręcznie administrator. Samo zgłoszenie nie zwiększa salda.\n\n'
    'Zamówienia produktów aktywowanych przez sklep realizujemy do 7 dni. Po zakupie podasz dane do wysyłki.'
)


class TopupInput(StatesGroup):
    amount = State()

class ShippingInput(StatesGroup):
    name = State()
    phone = State()
    email = State()
    locker_code = State()


def create_user_router(service, settings) -> Router:
    router = Router(name='users')
    demo = settings.demo_mode
    welcome = WELCOME if demo else REAL_WELCOME
    suffix = ' DEMO' if demo else ''

    def contact_markup(markup):
        username = settings.merchant_telegram_username
        if username:
            markup.inline_keyboard.insert(0, [InlineKeyboardButton(text='💬 Napisz do właściciela', url=f'https://t.me/{username}')])
        return markup

    def contact_text():
        if settings.merchant_telegram_username:
            return f'Aby wpłacić przez BLIK, napisz do <b>@{safe(settings.merchant_telegram_username)}</b>. Ustal kwotę i termin płatności bezpośrednio z właścicielem.\nNie przesyłaj kodów BLIK do tego bota.'
        return 'Kontakt właściciela nie został jeszcze ustawiony. Płatność BLIK będzie dostępna po skonfigurowaniu nicku.'

    async def register(event, referrer=None):
        user = event.from_user
        return await service.register(user.id, user.username, user.first_name, referrer)

    async def admin_notices(event, text, rows):
        for admin_id in settings.admin_ids:
            await notify(event.bot, admin_id, text, keyboard(rows, back='a:menu'))

    async def show_profile(event):
        u = await service.profile(event.from_user.id)
        await render(event, title('👤 TWÓJ PROFIL') + (
            f'🆔 ID: <code>{u.telegram_id}</code>\n👤 Nick: {"@" + safe(u.username) if u.username else safe(u.first_name)}\n'
            f'💰 Saldo{suffix}: {money(u.balance_grosze)}\n'
            + (f'🎁 Bonusy: {money(u.bonus_grosze)}\n' if demo else '')
            + f'🛍️ Zakupy: {u.purchase_count}\n🤝 Polecenia: {u.successful_referrals}\n'
            f'📅 Rejestracja: {u.created_at:%d.%m.%Y}\n📌 Status: {safe(u.status)}\n\n'
            + ('Bonusy DEMO są osobnym, niewydawalnym saldem.' if demo else 'Saldo jest uznawane po ręcznym potwierdzeniu wpłaty przez administratora.')
        ), keyboard([[('💳 Doładuj saldo', 'topups')], [('📜 Historia transakcji', 'transactions:0')], [('🤝 Mój link polecający', 'referrals')]]))

    async def show_referrals(event):
        stats = await service.referral_stats(event.from_user.id)
        bot_name = (await event.bot.get_me()).username
        link = f'https://t.me/{bot_name}?start=ref_{event.from_user.id}'
        markup = keyboard([[('📊 Statystyki', 'referrals')], [('📜 Zasady', 'referral_rules')]])
        markup.inline_keyboard.insert(0, [InlineKeyboardButton(text='🔗 Udostępnij link', url='https://t.me/share/url?' + urlencode({'url': link, 'text': 'Dołącz do MONSTAL 🎁'}))])
        await render(event, title('🤝 PROGRAM POLECEŃ') + (
            (f'🎁 Bonus: {money(settings.referral_bonus_grosze)} DEMO\n' if demo else 'Premie za polecenia są wyłączone w trybie rzeczywistych sald.\n')
            + f'👥 Zaproszeni: {stats["invited"]}\n✅ Skuteczne polecenia: {stats["successful"]}\n'
            + (f'💰 Zdobyte bonusy: {money(stats["earned_grosze"])}\n' if demo else '')
            + f'\nTwój link:\n{safe(link)}'
        ), markup)

    async def show_topup(event, topup):
        if not demo and topup.method == 'crypto' and topup.asset in ('LTC', 'XMR'):
            markup = keyboard([[('🔄 Sprawdź status', f'topup_status:{topup.id}')]])
            contact_markup(markup)
            text = title('🪙 WPŁATA ' + safe(topup.asset)) + 'Wpłaty w LTC i XMR są ustalane prywatnie. ' + (f'Napisz do <b>@{safe(settings.merchant_telegram_username)}</b>, podaj kwotę PLN i poczekaj na instrukcje. Nie wysyłaj środków na adres z wcześniejszej wyceny.' if settings.merchant_telegram_username else 'Prywatny kontakt właściciela nie jest jeszcze skonfigurowany.')
            await render(event, text, markup)
            return
        if not demo and settings.crypto_auto_rates and topup.method == 'crypto' and topup.status in ('awaiting_quote', 'awaiting_payment'):
            topup = await service.auto_quote_topup(event.from_user.id, topup.id, suppress_rate_error=True)
        rows = [[('🔄 Sprawdź status', f'topup_status:{topup.id}')]]
        if demo:
            text = title('💳 ZGŁOSZENIE DEMO') + (
                '<b>Symulacja — nie wykonuj żadnego przelewu i nie podawaj kodu BLIK.</b>\n\n'
                f'Metoda: {safe(topup.method.upper())} DEMO\n'
                + ('Sieć: MONSTAL-SIMULATOR (fikcyjna, bez portfeli)\n' if topup.method == 'crypto' else '')
                + f'Identyfikator: <code>{safe(topup.external_id)}</code>\n'
                f'Kwota: {money(topup.amount_grosze)}\nStatus: <b>{safe(topup.status)}</b>\n\n'
                'Testowe saldo pojawi się po zatwierdzeniu przez administratora.'
            )
            await render(event, text, keyboard(rows))
            return
        text = title('💳 TWOJA WPŁATA') + (
            f'ID zgłoszenia: <code>{safe(topup.id)}</code>\n'
            f'Do uznania w PLN: <b>{money(topup.amount_grosze)}</b>\n'
            f'Status: <b>{safe(topup.status)}</b>\n\n'
        )
        if topup.method == 'blik':
            text += contact_text() + '\n\nPo wykonaniu ustalonej płatności zgłoś ją przyciskiem poniżej. Administrator sprawdzi wpłatę i ręcznie uzna saldo.'
        else:
            text += f'Waluta: <b>{safe(topup.asset)}</b>\nSieć: <b>{safe(topup.network)}</b>\n'
            if topup.status == 'awaiting_quote':
                text += ('\nAutomatyczny kurs jest chwilowo niedostępny. Odśwież status później — bot sam obliczy kwotę. Jeszcze nie wysyłaj środków.'
                         if settings.crypto_auto_rates else '\nAdministrator ustala dokładną kwotę w kryptowalucie dla wybranej kwoty PLN. Poczekaj na wycenę i odśwież status. Jeszcze nie wysyłaj środków.')
            elif topup.crypto_amount:
                expires = topup.quote_expires_at
                expired = bool(expires and expires.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc))
                text += f'Kwota krypto: <b>{safe(topup.crypto_amount)} {safe(topup.asset)}</b>\n'
                if topup.rate_pln:
                    mode_label = 'minimum z ostatnich 24 godzin' if topup.rate_mode == 'low_24h' else 'kurs rynkowy z chwili aktualizacji'
                    text += f'Kurs: 1 {safe(topup.asset)} = {safe(topup.rate_pln)} PLN\nŹródło: {safe(topup.rate_source)}\nTryb: {mode_label}\n'
                    if topup.rate_source_updated_at:
                        text += f'Aktualizacja źródła: {topup.rate_source_updated_at:%d.%m.%Y %H:%M} UTC\n'
                if expires:
                    text += f'Wycena ważna do: {expires:%d.%m.%Y %H:%M} UTC\n'
                if expired and topup.status == 'awaiting_payment':
                    text += ('\n<b>Wycena wygasła, a aktualny kurs jest niedostępny. Nie wysyłaj środków. Odśwież status później.</b>'
                             if settings.crypto_auto_rates else '\n<b>Wycena wygasła. Nie wysyłaj środków. Poproś administratora o nową wycenę.</b>')
                else:
                    if topup.status == 'awaiting_payment':
                        text += f'\nAdres odbiorcy:\n<code>{safe(topup.wallet_address)}</code>\n\n'
                        text += 'Wyślij wyłącznie wskazaną walutę przez wskazaną sieć. Opłata sieciowa nie powinna pomniejszać kwoty otrzymanej przez właściciela.\n'
                        rows.insert(0, [('✅ Wysłałem wpłatę', f'topup_submit:{topup.id}:{service.quote_token(topup)}')])
                    else:
                        text += '\nTo zgłoszenie nie oczekuje na kolejną wpłatę. Nie wysyłaj środków ponownie.'
            if topup.status == 'submitted':
                text += '\nZgłoszenie oczekuje na ręczne sprawdzenie przez administratora. Bot nie monitoruje blockchaina.'
        if topup.method == 'blik' and topup.status == 'awaiting_payment':
            rows.insert(0, [('✅ Płatność wykonana — zgłoś', f'topup_submit:{topup.id}')])
        if topup.status == 'approved':
            text += '\n\n✅ Administrator potwierdził wpłatę i uznał saldo.'
        if topup.status == 'rejected':
            text += '\n\nZgłoszenie odrzucone. Skontaktuj się z właścicielem w sprawie wyjaśnienia.'
        markup = keyboard(rows)
        if topup.method == 'blik' or topup.status in ('awaiting_quote', 'rejected'):
            contact_markup(markup)
        await render(event, text + '\n\nSamo kliknięcie przycisku nie zwiększa salda.', markup)

    async def manual_amounts(event, instrument):
        if not demo and instrument in ('LTC', 'XMR'):
            markup = keyboard([])
            contact_markup(markup)
            text = title('🪙 WPŁATA ' + instrument)
            if settings.merchant_telegram_username:
                text += (f'Aby wpłacić w {instrument}, napisz prywatnie do <b>@{safe(settings.merchant_telegram_username)}</b>. Podaj kwotę w PLN i poczekaj na indywidualne instrukcje. Nie wysyłaj krypto na adres pokazany w innym zgłoszeniu.')
            else:
                text += f'Wpłaty w {instrument} obsługujemy wyłącznie prywatnie. Administrator nie ustawił jeszcze nicku kontaktowego; spróbuj później.'
            await render(event, text, markup)
            return
        if instrument == 'BLIK':
            text = contact_text()
            configured = bool(settings.merchant_telegram_username)
        else:
            option = next((o for o in service.crypto_options() if o['code'] == instrument), None)
            if option is None:
                raise ValueError('Nieobsługiwana kryptowaluta.')
            configured = option['configured']
            text = f'Waluta: <b>{safe(option["name"])} ({instrument})</b>\nSieć: <b>{safe(option["network"])}</b>\n\n'
            if configured:
                text += ('Wybierz kwotę salda w PLN, np. 50 zł. Bot automatycznie obliczy kwotę kryptowaluty z kursu CoinGecko i pokaże adres oraz czas ważności wyceny.'
                         if settings.crypto_auto_rates else 'Wybierz kwotę salda w PLN. Administrator poda dokładną kwotę krypto i czas ważności wyceny. Adres do wpłaty pojawi się razem z wyceną.')
            else:
                text += 'Adres dla tej waluty nie został jeszcze skonfigurowany. Wpłaty są na razie niedostępne.'
        rows = [[(money(v), f'manual_amount:{instrument}:{v}')] for v in (1000, 2500, 5000, 10000)] if configured else []
        if configured:
            rows.append([('✍️ Inna kwota', f'manual_custom:{instrument}')])
        markup = keyboard(rows)
        if instrument == 'BLIK':
            contact_markup(markup)
        await render(event, title('💳 WPŁATA ' + instrument) + text, markup)

    async def manual_confirmation(event, instrument, value):
        if not 100 <= value <= 100_000:
            raise ValueError('Kwota musi mieścić się w zakresie 1–1000 zł.')
        text = f'Utworzyć zgłoszenie wpłaty <b>{instrument}</b> na saldo <b>{money(value)}</b>?\n\n'
        text += ('Płatność ustalasz bezpośrednio z właścicielem. Kodów BLIK nie podawaj botowi.' if instrument == 'BLIK'
                 else 'Bot automatycznie przeliczy PLN na kryptowalutę i pokaże kwotę, sieć oraz adres do wpłaty.' if settings.crypto_auto_rates
                 else 'Najpierw otrzymasz od administratora dokładną kwotę kryptowaluty i adres w odpowiedniej sieci.')
        await render(event, title('💳 POTWIERDZENIE WPŁATY') + text, keyboard([[('✅ Utwórz zgłoszenie', f'manual_create:{instrument}:{value}:{uuid4().hex}')]]))

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        payload = (message.text or '').split(maxsplit=1)
        referrer = None
        if len(payload) > 1 and payload[1].startswith('ref_'):
            try:
                referrer = int(payload[1][4:])
                if not 0 < referrer < 2**63:
                    referrer = None
            except ValueError:
                pass
        await register(message, referrer)
        banner = Path(__file__).resolve().parents[2] / 'assets' / 'start_banner.png'
        if banner.is_file():
            await message.answer_photo(FSInputFile(banner), caption=welcome, reply_markup=main_menu())
        else:
            await render(message, welcome, main_menu())

    @router.message(Command('cancel'))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await render(message, welcome, main_menu())

    @router.message(TopupInput.amount, F.text, ~F.text.startswith('/'))
    async def custom_amount(message: Message, state: FSMContext):
        data = await state.get_data()
        value = amount(message.text)
        if not 100 <= value <= 100_000:
            raise ValueError('Kwota musi mieścić się w zakresie 1–1000 zł.')
        await state.clear()
        if not demo:
            await manual_confirmation(message, data['instrument'], value)
        else:
            method = data['method']
            await render(message, title('💳 POTWIERDZENIE DEMO') + f'Zgłoszenie symulacji {method.upper()} na {money(value)}.\nNie wysyłaj pieniędzy.', keyboard([[('✅ Utwórz zgłoszenie DEMO', f'topup_create:{method}:{value}:{uuid4().hex}')]]))

    @router.message(ShippingInput.name, F.text, ~F.text.startswith('/'))
    async def shipping_name(message: Message, state: FSMContext):
        name = message.text.strip()
        if not 2 <= len(name) <= 120:
            await message.answer('Podaj imię i nazwisko (2–120 znaków).')
            return
        await state.update_data(shipping_name=name)
        await state.set_state(ShippingInput.phone)
        await message.answer('Podaj numer telefonu do kuriera (np. +48 600 000 000).')

    @router.message(ShippingInput.phone, F.text, ~F.text.startswith('/'))
    async def shipping_phone(message: Message, state: FSMContext):
        phone = message.text.strip()
        import re
        if not re.fullmatch(r'[+0-9() .-]{6,30}', phone):
            await message.answer('Podaj poprawny numer telefonu.')
            return
        await state.update_data(shipping_phone=phone)
        await state.set_state(ShippingInput.email)
        await message.answer('Podaj adres e-mail potrzebny do potwierdzenia zamówienia i kontaktu w sprawie dostawy.')

    @router.message(ShippingInput.email, F.text, ~F.text.startswith('/'))
    async def shipping_email(message: Message, state: FSMContext):
        import re
        email = message.text.strip()
        if len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            await message.answer('Podaj poprawny adres e-mail.')
            return
        await state.update_data(shipping_email=email)
        await state.set_state(ShippingInput.locker_code)
        await message.answer('Dostawa jest wyłącznie do paczkomatu InPost. Podaj kod wybranego paczkomatu, np. WAW01M. Nie podawaj adresu domowego.')

    @router.message(ShippingInput.locker_code, F.text, ~F.text.startswith('/'))
    async def shipping_locker_code(message: Message, state: FSMContext):
        import re
        locker_code = message.text.strip().upper()
        if not re.fullmatch(r'[A-Z0-9-]{5,8}', locker_code):
            await message.answer('Podaj prawidłowy kod paczkomatu InPost, np. WAW01M.')
            return
        data = await state.get_data()
        order = await service.save_shipping(message.from_user.id, data['shipping_order'], data['shipping_name'], data['shipping_phone'], data['shipping_email'], locker_code)
        await state.clear()
        await render(message, title('📮 DANE ZAPISANE') + f'Dane do dostawy InPost dla zamówienia <code>{order.id}</code> zapisano w zaszyfrowanej postaci. Realizacja do 7 dni.', keyboard([[('📦 Moje zamówienia', 'orders:0')]]))
        await admin_notices(message, f'📮 Dane do wysyłki dodano do zamówienia <code>{order.id}</code>. Otwórz panel /admin.', [[('📦 Otwórz zamówienie', f'a:order:{order.id}')]])

    @router.callback_query(~F.data.startswith('a:'))
    async def navigation(callback: CallbackQuery, state: FSMContext):
        data = callback.data or ''
        await register(callback)
        if not data.startswith(('topup_custom:', 'manual_custom:')):
            await state.clear()
        if data == 'menu':
            await render(callback, welcome, main_menu())
        elif data == 'profile':
            await show_profile(callback)
        elif data.startswith('shipping_start:'):
            order_id = data.split(':', 1)[1]
            await state.set_state(ShippingInput.name)
            await state.set_data({'shipping_order': order_id})
            await render(callback, title('📮 DANE DO WYSYŁKI') + 'Podaj dane do dostawy InPost. Zostaną zaszyfrowane i użyte wyłącznie do realizacji zamówienia. Zacznij od imienia i nazwiska odbiorcy.', keyboard([]))
        elif data == 'help':
            default = 'Skontaktuj się z administratorem sklepu. Nie podawaj kodów BLIK temu botowi.'
            support = await service.setting('support_text', default)
            markup = keyboard([])
            if not demo:
                contact_markup(markup)
            await render(callback, title('🛟 POMOC') + safe(support) + '\n\n/start — menu\n/cancel — anuluj formularz\n/admin — panel uprawnionych administratorów', markup)
        elif data == 'catalog':
            categories = await service.categories()
            await render(callback, title('🎁 KARTY PODARUNKOWE' + suffix) + ('Wybierz kategorię. Wszystkie pozycje to fikcyjne produkty testowe.' if demo else 'Wybierz dostępny produkt. Zamówienia realizujemy do 7 dni.'), keyboard([[('🎁 ' + c.name, f'category:{c.id}')] for c in categories]))
        elif data.startswith('category:'):
            products = await service.products(int(data.split(':')[1]))
            await render(callback, title('🎁 PRODUKTY' + suffix) + 'Wybierz produkt i sprawdź jego region.\n' + ('' if products else 'Brak dostępnych produktów.'), keyboard([[(f'{p.denomination} {p.currency} · {p.region} · {money(p.price_grosze)}', f'product:{p.id}')] for p in products] + [[('⬅️ Kategorie', 'catalog')]]))
        elif data.startswith('product:'):
            product = await service.product(int(data.split(':')[1]))
            if not product.active or (not demo and product.is_example):
                raise ValueError('Ten produkt nie jest obecnie dostępny.')
            desc = (f'Cena DEMO: {money(product.price_grosze)}\nDostępność: {product.stock}\n\n{safe(product.description)}\n\nFikcyjny produkt testowy.' if demo else f'Cena: {money(product.price_grosze)}\nDostępność: {product.stock}\n\n{safe(product.description)}\n\nZamówienia realizujemy do 7 dni. Dostawa wyłącznie do paczkomatu InPost; po zakupie poprosimy o imię i nazwisko, telefon, e-mail i kod paczkomatu.')
            await render(callback, title('🎁 ' + product.name) + f'Nominał: {safe(product.denomination)} {safe(product.currency)}\nRegion: <b>{safe(product.region)}</b>\n' + desc, keyboard([[('🛒 Kup DEMO' if demo else '🛒 Kup z salda', f'purchase_confirm:{product.id}')], [('⬅️ Nominały', f'category:{product.category_id}')]]))
        elif data.startswith('purchase_confirm:'):
            product = await service.product(int(data.split(':')[1]))
            label = 'Z testowego salda pobierzemy' if demo else 'Z salda pobierzemy'
            await render(callback, title('🛒 POTWIERDŹ ZAKUP' + suffix) + f'{safe(product.name)}\nNominał: {safe(product.denomination)} {safe(product.currency)}\nRegion: {safe(product.region)}\n\n{label} <b>{money(product.price_grosze)}</b>.\n' + ('' if demo else 'Po zakupie podasz e-mail oraz kod paczkomatu InPost. Dostawy są wyłącznie do paczkomatów; realizacja do 7 dni.'), keyboard([[('✅ Potwierdzam zakup' + suffix, f'purchase:{product.id}:{product.price_grosze}:{uuid4().hex}')], [('⬅️ Produkt', f'product:{product.id}')]]))
        elif data.startswith('purchase:'):
            _, product_id, expected_price, key = data.split(':')
            order = await service.purchase(callback.from_user.id, int(product_id), key, expected_price_grosze=int(expected_price))
            text = title('📦 ZAMÓWIENIE PRZYJĘTE') + f'ID: <code>{order.id}</code>\nProdukt: {safe(order.product_name)}\nKwota: {money(order.price_grosze)}\nStatus: <b>{safe(order.status)}</b>\n\n' + ('Administrator ręcznie zrealizuje zamówienie DEMO.' if demo else 'Zamówienia realizujemy do 7 dni. Podaj teraz dane do dostawy InPost.')
            await render(callback, text, keyboard([[('📦 Moje zamówienia', 'orders:0')]]))
            if not demo and order.shipping_required:
                await state.set_state(ShippingInput.name)
                await state.set_data({'shipping_order': order.id})
                await callback.message.answer('Podaj imię i nazwisko odbiorcy. Dane są przechowywane w zaszyfrowanej postaci i używane wyłącznie do realizacji zamówienia InPost.')
            await admin_notices(callback, f'📦 Zamówienie <code>{order.id}</code>\nUżytkownik: <code>{callback.from_user.id}</code>\n{safe(order.product_name)} · {money(order.price_grosze)}', [[('📦 Otwórz zamówienie', f'a:order:{order.id}')]])
        elif data.startswith('orders:'):
            page = max(0, int(data.split(':')[1]))
            orders = await service.orders(callback.from_user.id)
            selection = orders[page * 8:page * 8 + 8]
            rows = [[(f'{o.product_name[:28]} · {o.status}', f'order:{o.id}')] for o in selection]
            paging = []
            if page:
                paging.append(('⬅️', f'orders:{page - 1}'))
            if len(orders) > (page + 1) * 8:
                paging.append(('➡️', f'orders:{page + 1}'))
            if paging:
                rows.append(paging)
            await render(callback, title('📦 MOJE ZAMÓWIENIA') + (f'Strona {page + 1}' if orders else 'Nie masz jeszcze zamówień.'), keyboard(rows))
        elif data.startswith('order:'):
            order = next((o for o in await service.orders(callback.from_user.id) if o.id == data.split(':', 1)[1]), None)
            if order is None:
                raise ValueError('Nie znaleziono Twojego zamówienia.')
            due = f'\nTermin realizacji: do {order.fulfillment_due_at:%d.%m.%Y %H:%M} UTC' if order.fulfillment_due_at else ''
            rows = []
            if order.shipping_required and not order.delivery_ciphertext and order.status in ('pending', 'processing'):
                rows.append([('📮 Podaj dane do wysyłki', f'shipping_start:{order.id}')])
            rows.append([('⬅️ Moje zamówienia', 'orders:0')])
            await render(callback, title('📦 ZAMÓWIENIE' + suffix) + f'ID: <code>{order.id}</code>\nProdukt: {safe(order.product_name)}\nKwota: {money(order.price_grosze)}\nStatus: <b>{safe(order.status)}</b>\nData: {order.created_at:%d.%m.%Y %H:%M} UTC' + due + '\n' + (f'\nFikcyjny kod: <code>{safe(order.demo_code)}</code>\nKod nie jest aktywną kartą podarunkową.' if order.demo_code else ''), keyboard(rows))
        elif data.startswith('transactions:'):
            page = max(0, int(data.split(':')[1]))
            items = await service.transactions(callback.from_user.id)
            text = '\n\n'.join(f'{t.created_at:%d.%m.%Y %H:%M} UTC · {safe(t.kind)}\n{money(t.amount_grosze)} → saldo {"bonusowe" if t.kind.startswith("referral_") else "DEMO" if demo else "PLN"} {money(t.balance_after_grosze)}' for t in items[page * 8:page * 8 + 8]) or 'Brak operacji.'
            rows = []
            if page:
                rows.append([('⬅️ Poprzednie', f'transactions:{page - 1}')])
            if len(items) > (page + 1) * 8:
                rows.append([('➡️ Następne', f'transactions:{page + 1}')])
            await render(callback, title('📜 HISTORIA TRANSAKCJI' + suffix) + text, keyboard(rows))
        elif data == 'topups':
            intro = 'Wyłącznie symulacja. Nie wysyłaj pieniędzy ani kodów BLIK.' if demo else 'Wpłaty są sprawdzane ręcznie. Saldo zostanie uznane dopiero po potwierdzeniu przez administratora.'
            await render(callback, title('💳 DOŁADUJ SALDO') + 'Wybierz metodę doładowania:\n\n<b>' + intro + '</b>', keyboard([[('🟣 BLIK' + suffix, 'topup_method:blik')], [('🪙 Crypto' + suffix, 'topup_method:crypto')], [('📜 Moje zgłoszenia', 'my_topups')]]))
        elif data.startswith('topup_method:'):
            method = data.split(':')[1]
            if method not in ('blik', 'crypto'):
                raise ValueError('Nieprawidłowa metoda.')
            if not demo and method == 'blik':
                await manual_amounts(callback, 'BLIK')
            elif not demo:
                options = service.crypto_options()
                rows = [[(f'{o["code"]} · {o["name"]}' + (' · kontakt prywatny' if o.get('contact_only') else '' if o['configured'] else ' · niedostępne'), f'crypto_asset:{o["code"]}')] for o in options]
                description = ('Wybierz walutę i kwotę PLN. Bot obliczy dokładną ilość krypto według kursu CoinGecko. Wpłatę sprawdza ręcznie administrator.'
                               if settings.crypto_auto_rates else 'Każda waluta ma osobną, określoną sieć. Przed wpłatą administrator ustala dokładną kwotę kryptowaluty dla wybranej kwoty PLN.')
                await render(callback, title('🪙 WYBIERZ KRYPTOWALUTĘ') + description, keyboard(rows))
            else:
                await render(callback, title('💳 ' + method.upper() + ' DEMO') + 'To symulacja. Wybierz kwotę testową; administrator zatwierdzi zgłoszenie.\nNie podawaj kodu BLIK. Nie ma adresu portfela ani transferu kryptowaluty.', keyboard([[(money(value), f'topup_amount:{method}:{value}')] for value in (1000, 2500, 5000, 10000)] + [[('✍️ Inna kwota', f'topup_custom:{method}')]]))
        elif data.startswith('crypto_asset:') and not demo:
            await manual_amounts(callback, data.split(':')[1])
        elif data.startswith('manual_amount:') and not demo:
            _, instrument, value = data.split(':')
            await manual_confirmation(callback, instrument, int(value))
        elif data.startswith('manual_custom:') and not demo:
            instrument = data.split(':')[1]
            if instrument not in {'BLIK', 'BTC', 'ETH', 'USDC', 'SOL', 'LTC', 'XMR'}:
                raise ValueError('Nieprawidłowa metoda.')
            await state.set_state(TopupInput.amount)
            await state.update_data(instrument=instrument)
            await render(callback, title('✍️ KWOTA WPŁATY PLN') + 'Podaj kwotę salda w złotych (1–1000), np. 25,50.\n/cancel — anuluj.', keyboard([]))
        elif data.startswith('manual_create:') and not demo:
            _, instrument, value, key = data.split(':')
            if instrument not in {'BLIK', 'BTC', 'ETH', 'USDC', 'SOL', 'LTC', 'XMR'}:
                raise ValueError('Nieprawidłowa metoda.')
            topup = await service.create_topup(callback.from_user.id, int(value), 'blik' if instrument == 'BLIK' else 'crypto', key, asset=None if instrument == 'BLIK' else instrument)
            await show_topup(callback, topup)
            await admin_notices(callback, f'💳 Nowe zgłoszenie wpłaty <code>{topup.id}</code>\nUżytkownik: <code>{callback.from_user.id}</code>\nSaldo do uznania: {money(topup.amount_grosze)}\nMetoda: {safe(instrument)}\nWpłata wymaga ręcznej weryfikacji.', [[('💳 Otwórz wpłatę', f'a:topup:{topup.id}')]])
        elif data.startswith('topup_submit:') and not demo:
            parts = data.split(':')
            if len(parts) not in (2, 3):
                raise ValueError('Nieprawidłowe zgłoszenie wpłaty. Otwórz szczegóły ponownie.')
            topup = await service.submit_topup(callback.from_user.id, parts[1], quote_token=parts[2] if len(parts) == 3 else None)
            await show_topup(callback, topup)
            await admin_notices(callback, f'🔎 Użytkownik zgłosił wykonanie wpłaty <code>{topup.id}</code>.\nZweryfikuj faktyczny wpływ {money(topup.amount_grosze)} przed uznaniem salda.', [[('💳 Sprawdź wpłatę', f'a:topup:{topup.id}')]])
        elif data.startswith('topup_amount:') and demo:
            _, method, value = data.split(':')
            await render(callback, title('💳 POTWIERDZENIE DEMO') + f'Utworzyć zgłoszenie {method.upper()} na {money(int(value))}?\nWyłącznie symulacja.', keyboard([[('✅ Utwórz zgłoszenie DEMO', f'topup_create:{method}:{value}:{uuid4().hex}')]]))
        elif data.startswith('topup_custom:') and demo:
            method = data.split(':')[1]
            if method not in ('blik', 'crypto'):
                raise ValueError('Nieprawidłowa metoda.')
            await state.set_state(TopupInput.amount)
            await state.update_data(method=method)
            await render(callback, title('✍️ INNA KWOTA DEMO') + 'Podaj kwotę testową w złotych (1–1000), np. 25,50.\n/cancel — anuluj.', keyboard([]))
        elif data.startswith('topup_create:') and demo:
            _, method, value, key = data.split(':')
            topup = await service.create_topup(callback.from_user.id, int(value), method, key)
            await show_topup(callback, topup)
            await admin_notices(callback, f'💳 Zgłoszenie DEMO <code>{topup.id}</code>\nUżytkownik <code>{callback.from_user.id}</code> · {money(topup.amount_grosze)} · {safe(topup.method)}', [[('💳 Otwórz zgłoszenie', f'a:topup:{topup.id}')]])
        elif data.startswith('topup_status:'):
            await show_topup(callback, await service.topup(callback.from_user.id, data.split(':', 1)[1]))
        elif data == 'my_topups':
            topups = await service.topups(callback.from_user.id)
            await render(callback, title('💳 MOJE ZGŁOSZENIA' + suffix) + ('Ostatnie 30 zgłoszeń.' if topups else 'Brak zgłoszeń.'), keyboard([[(f'{money(t.amount_grosze)} · {t.status}', f'topup_status:{t.id}')] for t in topups[:30]]))
        elif data == 'referrals':
            await show_referrals(callback)
        elif data == 'referral_rules':
            text = f'• Jeden użytkownik może mieć jednego polecającego.\n• Nie można polecić siebie.\n• Bonus {money(settings.referral_bonus_grosze)} DEMO naliczamy raz, po pierwszym ukończonym zakupie poleconego.\n• Anulowane zakupy nie kwalifikują się.\n• Zwrot kwalifikującego zakupu cofa bonus i skuteczne polecenie. Nie przyznajemy go ponownie.\n• Bonusy są niewydawalnym saldem demonstracyjnym.' if demo else 'W trybie rzeczywistych sald link może zapisać osobę polecającą, ale premie pieniężne nie są naliczane. Program premiowy wymaga odrębnego wdrożenia zasad.'
            await render(callback, title('📜 ZASADY POLECEŃ') + text, keyboard([[('🤝 Polecenia', 'referrals')]]))
        else:
            raise ValueError('Ten przycisk jest nieaktualny. Otwórz /start.')
        await callback.answer()

    return router
