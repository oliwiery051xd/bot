import asyncio
from datetime import datetime, timezone
import json
import time
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.keyboards import keyboard
from app.middlewares.admin_security import AdminSecurity
from app.services.manual_payments import crypto_amount
from app.utils.formatting import amount, money, safe, title
from app.utils.telegram import notify, render


class AdminInput(StatesGroup):
    value = State()
    confirmation = State()


ADMIN_ROWS = [
    [('👥 Użytkownicy', 'a:users'), ('🎁 Produkty', 'a:products:0')],
    [('📦 Zamówienia', 'a:orders:0'), ('💳 Doładowania DEMO', 'a:topups:0')],
    [('💰 Salda testowe', 'a:balances'), ('📊 Statystyki', 'a:stats')],
    [('📢 Powiadomienia', 'a:broadcast'), ('⚙️ Ustawienia', 'a:settings')],
]


def create_admin_router(service, settings) -> Router:
    router = Router(name='admins')
    security = AdminSecurity(settings)
    router.message.outer_middleware(security)
    router.callback_query.outer_middleware(security)
    demo = settings.demo_mode
    suffix = ' DEMO' if demo else ''
    admin_rows = [[(label.replace(' DEMO', suffix).replace('Salda testowe', 'Salda testowe' if demo else 'Salda PLN'), callback) for label, callback in row] for row in ADMIN_ROWS]

    def authorize(telegram_id):
        if not settings.is_admin(telegram_id):
            raise ValueError('Brak uprawnień administratora. Dostęp jest przyznawany wyłącznie po Telegram ID.')

    async def ask(event, state, operation, prompt, **context):
        await state.set_state(AdminInput.value)
        await state.set_data({'operation': operation, **context})
        await render(event, title('🛡️ FORMULARZ ADMINISTRATORA') + prompt + '\n\n/cancel — anuluj. Po wprowadzeniu danych zobaczysz potwierdzenie.', keyboard([], back='a:menu'))

    async def confirm(event, state, operation, summary, **payload):
        token = uuid4().hex
        await state.set_state(AdminInput.confirmation)
        await state.set_data({'operation': operation, 'payload': payload, 'token': token, 'expires_at': time.monotonic() + 600})
        await render(event, title('⚠️ POTWIERDZENIE ADMINISTRATORA') + summary, keyboard([[('✅ Potwierdzam operację', f'a:execute:{token}')]], back='a:menu'))

    async def rate_details():
        if not settings.crypto_auto_rates:
            return 'Wyceny krypto: ręczne'
        mode = 'bieżący kurs' if settings.rate_price_mode == 'current' else 'minimum z ostatnich 24 h'
        details = f'Wyceny krypto: automatyczne · {mode}\nOdświeżanie: co {settings.rate_refresh_seconds} s\n'
        # Inspect persisted cache metadata only. Opening settings never makes an
        # external rate request and never exposes API keys or the cache payload.
        raw = await service.setting('crypto_rate_cache_v1', '')
        try:
            if not raw or len(raw) > 32768:
                raise ValueError
            batch = json.loads(raw)['snapshots']
            dates = [datetime.fromisoformat(item['source_updated_at']) for item in batch.values()]
            if not dates:
                raise ValueError
            updated = min(value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc) for value in dates)
            source = next(iter(batch.values()))['source']
            details += f'Źródło: {safe(str(source)[:100])}\nOstatnia aktualizacja źródła: {updated:%d.%m.%Y %H:%M} UTC'
        except (KeyError, ValueError, TypeError, AttributeError):
            details += 'Źródło: CoinGecko\nJeszcze nie pobrano kursów.'
        return details

    async def show_user(event, telegram_id):
        user = await service.admin_user(event.from_user.id, telegram_id)
        transactions = await service.transactions(telegram_id)
        orders = await service.orders(telegram_id)
        text = title('👥 UŻYTKOWNIK') + (
            f'ID: <code>{user.telegram_id}</code>\nNick: {"@" + safe(user.username) if user.username else "—"}\n'
            f'Imię: {safe(user.first_name)}\nRejestracja: {user.created_at:%d.%m.%Y}\n'
            f'Saldo{suffix}: {money(user.balance_grosze)}\nBonusy: {money(user.bonus_grosze)}\n'
            f'Zamówienia: {len(orders)}\nZakupy: {user.purchase_count}\n'
            f'Obrót{suffix}: {money(user.total_spent_grosze)}\nStatus: {safe(user.status)}\n\n'
            '<b>Ostatnie operacje</b>\n' + ('\n'.join(f'{t.created_at:%d.%m} · {safe(t.kind)} · {money(t.amount_grosze)}' for t in transactions[:8]) or 'Brak')
        )
        status = 'active' if user.status == 'blocked' else 'blocked'
        await render(event, text, keyboard([[('💰 Korekta salda' + suffix, f'a:balance:{telegram_id}')], [('📜 Historia operacji', f'a:history:{telegram_id}:0')], [('✅ Odblokuj' if status == 'active' else '⛔ Zablokuj', f'a:status:{telegram_id}:{status}')]], back='a:menu'))

    async def show_order(event, order_id):
        order = next((o for o in await service.admin_orders(event.from_user.id) if o.id == order_id), None)
        if order is None:
            raise ValueError('Nie znaleziono zamówienia.')
        telegram_id = await service.telegram_for_user(order.user_id)
        rows = []
        if order.status == 'pending':
            rows.append([('🛠️ Przyjmij do realizacji', f'a:oconf:{order.id}:processing')])
        if order.status in ('pending', 'processing'):
            rows.append([('✅ Oznacz wysyłkę jako zrealizowaną' if not demo else '✅ Ukończ — kod DEMO', f'a:oconf:{order.id}:completed')])
            rows.append([('❌ Anuluj i zwróć saldo', f'a:oconf:{order.id}:cancelled')])
        if order.status == 'completed':
            rows.append([('↩️ Zwróć saldo', f'a:oconf:{order.id}:refunded')])
        rows.append([('📦 Lista zamówień', 'a:orders:0')])
        due = f'\nTermin realizacji do: {order.fulfillment_due_at:%d.%m.%Y %H:%M} UTC' if order.fulfillment_due_at else ''
        if order.shipping_required and order.delivery_ciphertext:
            rows.insert(0, [('🔐 Dane wysyłkowe', f'a:shipping:{order.id}')])
        caption = 'ZAMÓWIENIE DEMO' if demo else 'ZAMÓWIENIE'
        await render(event, title('📦 ' + caption) + f'ID: <code>{order.id}</code>\nUżytkownik: <code>{telegram_id}</code>\nProdukt: {safe(order.product_name)}\nKwota: {money(order.price_grosze)}\nStatus: <b>{safe(order.status)}</b>' + due + '\nDane wysyłkowe: ' + ('otrzymane' if order.delivery_ciphertext else 'oczekują na użytkownika' if order.shipping_required else 'nie wymagane') + (f'\n\nKod fikcyjny: <code>{safe(order.demo_code)}</code>' if order.demo_code else ''), keyboard(rows, back='a:menu'))

    async def show_topup(event, topup_id):
        topup = next((t for t in await service.admin_topups(event.from_user.id) if t.id == topup_id), None)
        if topup is None:
            raise ValueError('Nie znaleziono zgłoszenia.')
        telegram_id = await service.telegram_for_user(topup.user_id)
        rows = []
        if demo and topup.status == 'pending':
            rows = [[('✅ Zatwierdź DEMO', f'a:tconf:{topup.id}:yes')], [('❌ Odrzuć', f'a:tconf:{topup.id}:no')]]
        if not demo:
            if not settings.crypto_auto_rates and topup.method == 'crypto' and topup.status in ('awaiting_quote', 'awaiting_payment'):
                rows.append([('💱 Ustal kwotę kryptowaluty', f'a:tquote:{topup.id}')])
            if topup.status == 'submitted':
                rows.append([('✅ Zweryfikowałem wpływ — uznaj saldo', f'a:tapprove:{topup.id}')])
            if topup.status in ('awaiting_quote', 'awaiting_payment', 'submitted'):
                rows.append([('❌ Odrzuć zgłoszenie', f'a:tconf:{topup.id}:no')])
        text = title('💳 DOŁADOWANIE' + suffix) + f'ID: <code>{topup.id}</code>\nUżytkownik: <code>{telegram_id}</code>\nKwota do uznania: <b>{money(topup.amount_grosze)}</b>\nMetoda: {safe(topup.method)}\nStatus: <b>{safe(topup.status)}</b>\n'
        if demo:
            text += '\nWyłącznie symulacja, bez sprawdzania prawdziwego przelewu.'
        else:
            if topup.method == 'crypto':
                text += f'Waluta: {safe(topup.asset)}\nSieć: {safe(topup.network)}\nAdres odbiorcy: <code>{safe(topup.wallet_address)}</code>\n'
                if topup.crypto_amount:
                    text += f'Kwota krypto: <b>{safe(topup.crypto_amount)} {safe(topup.asset)}</b>\n'
                if topup.quote_expires_at:
                    text += f'Wycena ważna do: {topup.quote_expires_at:%d.%m.%Y %H:%M} UTC\n'
                if topup.rate_pln:
                    mode = 'bieżący' if topup.rate_mode == 'current' else 'minimum z 24 h'
                    text += f'Kurs: 1 {safe(topup.asset)} = <b>{safe(topup.rate_pln)} PLN</b> ({mode})\nŹródło: {safe(topup.rate_source)}\n'
                    if topup.rate_source_updated_at:
                        text += f'Aktualizacja źródła: {topup.rate_source_updated_at:%d.%m.%Y %H:%M} UTC\n'
            if topup.confirmation_reference:
                text += f'Referencja potwierdzenia: <code>{safe(topup.confirmation_reference)}</code>\n'
            text += '\n<b>Sprawdź rzeczywisty wpływ środków przed uznaniem salda.</b> Deklaracja użytkownika nie jest potwierdzeniem płatności. Bot nie monitoruje banku ani blockchaina.'
        await render(event, text, keyboard(rows, back='a:menu'))

    @router.message(Command('admin'))
    async def admin_command(message: Message, state: FSMContext):
        authorize(message.from_user.id)
        await state.clear()
        await render(message, title('🛡️ ADMIN PANEL') + f'MONSTAL{suffix} · wybierz sekcję.', keyboard(admin_rows))

    @router.message(AdminInput.value, F.text, ~F.text.startswith('/'))
    async def admin_value(message: Message, state: FSMContext):
        authorize(message.from_user.id)
        data = await state.get_data()
        op, text = data.get('operation'), (message.text or '').strip()
        if op == 'topup_quote':
            if settings.crypto_auto_rates:
                await state.clear()
                raise ValueError('Wyceny krypto są obliczane automatycznie z kursu dostawcy. Ręczne nadpisywanie jest wyłączone.')
            topup = next((t for t in await service.admin_topups(message.from_user.id) if t.id == data['topup_id']), None)
            if topup is None:
                raise ValueError('Zgłoszenie nie istnieje.')
            value = crypto_amount(topup.asset, text)
            await confirm(message, state, 'topup_quote', f'Wpłata: <code>{safe(topup.id)}</code>\nSaldo do uznania: <b>{money(topup.amount_grosze)}</b>\nDokładna kwota krypto: <b>{safe(value)} {safe(topup.asset)}</b>\nSieć: {safe(topup.network)}\nWycena będzie ważna przez 30 minut. Kurs ustalasz ręcznie.', topup_id=topup.id, crypto_amount=value)
        elif op == 'topup_approve':
            if not 3 <= len(text) <= 150 or text.isdigit() and len(text) == 6:
                raise ValueError('Podaj referencję faktycznie otrzymanej wpłaty, a nie kod BLIK. Dla krypto użyj identyfikatora sieci/transakcji.')
            topup = next((t for t in await service.admin_topups(message.from_user.id) if t.id == data['topup_id']), None)
            if topup is None:
                raise ValueError('Zgłoszenie nie istnieje.')
            target = await service.telegram_for_user(topup.user_id)
            await confirm(message, state, 'topup_approve', f'Wpłata: <code>{safe(topup.id)}</code>\nUżytkownik: <code>{target}</code>\nSaldo do uznania: <b>{money(topup.amount_grosze)}</b>\nMetoda: {safe(topup.asset or "BLIK")}\nPotwierdzenie: <code>{safe(text)}</code>\n\n<b>Potwierdzasz sprawdzenie faktycznego wpływu środków i ręczne uznanie salda PLN.</b>', topup_id=topup.id, reference=text)
        elif op == 'find_user':
            target = int(text)
            await state.clear()
            await show_user(message, target)
        elif op == 'balance':
            if 'target' in data:
                target, delta = int(data['target']), amount(text, signed=True)
            else:
                parts = text.split(maxsplit=1)
                if len(parts) != 2:
                    raise ValueError('Podaj: Telegram_ID kwota_zmiany, np. 123456789 25,50 lub 123456789 -10,00.')
                target, delta = int(parts[0]), amount(parts[1], signed=True)
            await service.admin_user(message.from_user.id, target)
            await confirm(message, state, 'balance', f'Użytkownik <code>{target}</code>\nZmiana salda{suffix}: <b>{money(delta)}</b>', target=target, delta=delta, request_key=uuid4().hex)
        elif op == 'category':
            if not 1 <= len(text) <= 100:
                raise ValueError('Nazwa kategorii musi mieć od 1 do 100 znaków.')
            await confirm(message, state, 'category', f'Utworzyć kategorię <b>{safe(text)}</b>?', name=text)
        elif op == 'product_new':
            fields = dict(data.get('fields', {}))
            steps = [
                ('name', 'Podaj nazwę produktu, np. Steam.'),
                ('category_id', 'Podaj ID kategorii z listy pokazanej na początku.'),
                ('denomination', 'Podaj nominał, np. 5 albo 10.'),
                ('currency', 'Podaj walutę nominału, np. USD lub PLN.'),
                ('region', 'Podaj region realizacji, np. Polska albo USA.'),
                ('price_grosze', 'Podaj cenę w PLN, np. 25,50.'),
                ('stock', 'Podaj liczbę dostępnych sztuk.'),
                ('description', 'Podaj opis produktu.'),
            ]
            step = int(data.get('step', 0))
            field = steps[step][0]
            value = amount(text) if field == 'price_grosze' else int(text) if field in ('category_id', 'stock') else text
            if field == 'category_id' and value not in [c.id for c in await service.admin_categories(message.from_user.id)]:
                raise ValueError('Wybierz istniejące ID kategorii.')
            if field == 'stock' and value < 0:
                raise ValueError('Liczba sztuk nie może być ujemna.')
            if field == 'currency' and (len(text) != 3 or not text.isascii() or not text.isalpha()):
                raise ValueError('Podaj trzyliterową walutę, np. USD.')
            fields[field] = value.upper() if field == 'currency' else value
            if step + 1 < len(steps):
                await state.update_data(fields=fields, step=step + 1)
                await render(message, title(f'➕ PRODUKT — KROK {step + 2}/8') + steps[step + 1][1], keyboard([], back='a:menu'))
            else:
                fields['active'] = True
                summary = f"Nazwa: {safe(fields['name'])}\nKategoria: {fields['category_id']}\nNominał: {safe(fields['denomination'])} {safe(fields['currency'])}\nRegion: {safe(fields['region'])}\nCena: {money(fields['price_grosze'])}\nSztuki: {fields['stock']}\n{safe(fields['description'])}"
                await confirm(message, state, 'product_new', summary, fields=fields)
        elif op == 'product_edit':
            field = data['field']
            value = amount(text) if field == 'price_grosze' else int(text) if field == 'stock' else text
            await confirm(message, state, 'product_edit', f'Produkt #{data["product_id"]}\nPole: {safe(field)}\nNowa wartość: {safe(money(value) if field == "price_grosze" else value)}', product_id=data['product_id'], fields={field: value})
        elif op == 'support':
            if not 1 <= len(text) <= 2000:
                raise ValueError('Tekst pomocy musi mieć od 1 do 2000 znaków.')
            await confirm(message, state, 'support', 'Zapisać tekst pomocy?\n\n' + safe(text), value=text)
        elif op == 'broadcast':
            if not 1 <= len(text) <= 3000:
                raise ValueError('Powiadomienie musi mieć od 1 do 3000 znaków.')
            count = len(await service.broadcast_recipients(message.from_user.id))
            await confirm(message, state, 'broadcast', f'Wysłać poniższe powiadomienie do {count} aktywnych użytkowników?\n\n{safe(text)}', text=text)
        else:
            await state.clear()
            raise ValueError('Formularz wygasł. Otwórz /admin.')

    @router.callback_query(F.data.startswith('a:'))
    async def admin_navigation(callback: CallbackQuery, state: FSMContext):
        authorize(callback.from_user.id)
        data = callback.data.split(':')
        action = data[1]
        if not demo and action in {'products', 'product', 'pnew', 'pedit', 'ptoggle', 'pdelete', 'categories', 'cnew', 'orders', 'order', 'oconf', 'orun'}:
            await state.clear()
            await render(callback, title('🛠️ SPRZEDAŻ W PRZYGOTOWANIU') + 'Rzeczywiste wpłaty i salda działają ręcznie. Sprzedaż kart i ich realizacja wymagają osobnego wdrożenia. Produkty i kody DEMO nie mogą obciążać prawdziwego salda.', keyboard([], back='a:menu'))
            await callback.answer()
            return
        if action == 'menu':
            await state.clear()
            await render(callback, title('🛡️ ADMIN PANEL') + f'MONSTAL{suffix} · wybierz sekcję.', keyboard(admin_rows))
        elif action == 'users':
            await ask(callback, state, 'find_user', 'Podaj Telegram ID użytkownika.')
        elif action == 'balances':
            await ask(callback, state, 'balance', 'Podaj Telegram ID i zmianę salda w złotych.\nPrzykład: <code>123456789 25,50</code>\nKorekta ujemna: <code>123456789 -10,00</code>')
        elif action == 'balance':
            await ask(callback, state, 'balance', f'Zmiana salda{suffix} dla <code>{int(data[2])}</code>. Podaj kwotę w złotych, np. 25,50 albo -10,00.', target=int(data[2]))
        elif action == 'user':
            await show_user(callback, int(data[2]))
        elif action == 'history':
            target, page = int(data[2]), max(0, int(data[3]))
            await service.admin_user(callback.from_user.id, target)
            history = await service.transactions(target)
            rows = []
            if page:
                rows.append([('⬅️ Poprzednie', f'a:history:{target}:{page - 1}')])
            if len(history) > (page + 1) * 10:
                rows.append([('➡️ Następne', f'a:history:{target}:{page + 1}')])
            await render(callback, title('📜 HISTORIA SALDA' + suffix) + '\n\n'.join(f'{t.created_at:%d.%m.%Y %H:%M} UTC · {safe(t.kind)}\n{money(t.amount_grosze)} → saldo {"bonusowe" if t.kind.startswith("referral_") else "DEMO" if demo else "PLN"} {money(t.balance_after_grosze)}' for t in history[page * 10:page * 10 + 10]), keyboard(rows, back='a:menu'))
        elif action == 'status':
            target, status = int(data[2]), data[3]
            if status not in ('active', 'blocked'):
                raise ValueError('Nieprawidłowy status.')
            await confirm(callback, state, 'status', f'Zmienić status użytkownika <code>{target}</code> na <b>{safe(status)}</b>?', target=target, status=status)
        elif action == 'products':
            page = max(0, int(data[2]))
            products = await service.admin_products(callback.from_user.id)
            rows = [[(f'#{p.id} {p.name[:25]} · {"✅" if p.active else "🙈"}', f'a:product:{p.id}')] for p in products[page * 8:page * 8 + 8]]
            if page:
                rows.append([('⬅️ Poprzednie', f'a:products:{page - 1}')])
            if len(products) > (page + 1) * 8:
                rows.append([('➡️ Następne', f'a:products:{page + 1}')])
            rows += [[('➕ Dodaj produkt' + suffix, 'a:pnew')], [('🗂️ Kategorie', 'a:categories')]]
            await render(callback, title('🎁 ZARZĄDZANIE PRODUKTAMI') + 'Ukryte produkty pozostają dostępne w historii zamówień.', keyboard(rows, back='a:menu'))
        elif action == 'product':
            product = next((p for p in await service.admin_products(callback.from_user.id) if p.id == int(data[2])), None)
            if product is None:
                raise ValueError('Produkt nie istnieje.')
            rows = [[('💰 Cena', f'a:pedit:{product.id}:price_grosze'), ('💵 Nominał', f'a:pedit:{product.id}:denomination')], [('🌍 Region', f'a:pedit:{product.id}:region'), ('📦 Sztuki', f'a:pedit:{product.id}:stock')], [('✍️ Nazwa', f'a:pedit:{product.id}:name'), ('📝 Opis', f'a:pedit:{product.id}:description')], [('🙈 Ukryj' if product.active else '✅ Aktywuj', f'a:ptoggle:{product.id}')], [('🗑️ Usuń', f'a:pdelete:{product.id}')]]
            await render(callback, title('🎁 ' + product.name) + f'ID: {product.id}\nKategoria: {product.category_id}\nNominał: {safe(product.denomination)} {safe(product.currency)}\nRegion: {safe(product.region)}\nCena: {money(product.price_grosze)}\nSztuki: {product.stock}\nAktywny: {product.active}\nPrzykładowy wpis: {product.is_example}\nWymaga wysyłki: {product.requires_shipping}\n\n{safe(product.description)}', keyboard(rows, back='a:menu'))
        elif action == 'categories':
            categories = await service.admin_categories(callback.from_user.id)
            await render(callback, title('🗂️ KATEGORIE') + '\n'.join(f'{c.id} · {safe(c.name)}' for c in categories), keyboard([[('➕ Dodaj kategorię', 'a:cnew')]], back='a:menu'))
        elif action == 'cnew':
            await ask(callback, state, 'category', 'Podaj nazwę nowej kategorii.')
        elif action == 'pnew':
            categories = await service.admin_categories(callback.from_user.id)
            if not categories:
                raise ValueError('Najpierw dodaj kategorię: Produkty → Kategorie → Dodaj kategorię.')
            await ask(callback, state, 'product_new', 'Krok 1/8. Podaj nazwę produktu. Kolejne dane wpiszesz pojedynczo.\n\nKategorie (ID):\n' + '\n'.join(f'{c.id} · {safe(c.name)}' for c in categories), step=0, fields={})
        elif action == 'pedit':
            field = data[3]
            prompts = {'price_grosze': 'cenę testową w PLN (np. 25,50)', 'denomination': 'nominał', 'region': 'region realizacji', 'stock': 'liczbę sztuk (liczba całkowita ≥ 0)', 'name': 'nazwę produktu', 'description': 'opis produktu'}
            if field not in prompts:
                raise ValueError('Nieprawidłowe pole produktu.')
            await ask(callback, state, 'product_edit', f'Podaj {prompts[field]}.', product_id=int(data[2]), field=field)
        elif action == 'ptoggle':
            product = next((p for p in await service.admin_products(callback.from_user.id) if p.id == int(data[2])), None)
            if product is None:
                raise ValueError('Produkt nie istnieje.')
            fields = {'active': not product.active}
            if not product.active and product.is_example:
                fields['is_example'] = False
            await confirm(callback, state, 'product_edit', f'Zmienić aktywność produktu #{product.id} na {not product.active}?' + ('\n\nTo opublikuje produkt w katalogu prawdziwych zakupów. Upewnij się, że masz towar do wysyłki.' if fields.get('is_example') is False else ''), product_id=product.id, fields=fields)
        elif action == 'pdelete':
            await confirm(callback, state, 'product_delete', f'Usunąć produkt #{int(data[2])}?\nProdukt powiązany z zamówieniami nie zostanie usunięty.', product_id=int(data[2]))
        elif action == 'orders':
            page = max(0, int(data[2]))
            orders = await service.admin_orders(callback.from_user.id)
            orders = sorted(orders, key=lambda o: (o.status not in ('pending', 'processing'), -o.created_at.timestamp()))
            rows = [[(f'{o.id[:8]} · {o.status} · {money(o.price_grosze)}', f'a:order:{o.id}')] for o in orders[page * 8:page * 8 + 8]]
            if page:
                rows.append([('⬅️ Poprzednie', f'a:orders:{page - 1}')])
            if len(orders) > (page + 1) * 8:
                rows.append([('➡️ Następne', f'a:orders:{page + 1}')])
            await render(callback, title('📦 ZAMÓWIENIA') + 'Najpierw oczekujące i w realizacji.', keyboard(rows, back='a:menu'))
        elif action == 'order':
            await show_order(callback, data[2])
        elif action == 'shipping':
            details = await service.shipping_details(callback.from_user.id, data[2])
            if not details:
                raise ValueError('Brak danych wysyłkowych.')
            await render(callback, title('🔐 DANE DO WYSYŁKI') + f'Zamówienie: <code>{safe(data[2])}</code>\nImię i nazwisko: {safe(details["name"])}\nTelefon: {safe(details["phone"])}\nE-mail: {safe(details["email"])}\nPaczkomat InPost: {safe(details["locker_code"])}\n\nDane są poufne. Użyj ich wyłącznie do realizacji tej wysyłki.', keyboard([[('⬅️ Zamówienie', f'a:order:{data[2]}')]], back='a:menu'))
        elif action == 'oconf':
            if data[3] not in ('processing', 'completed', 'cancelled', 'refunded'):
                raise ValueError('Nieprawidłowa operacja.')
            confirmation = 'Ukończenie wyśle kod wyłącznie w trybie DEMO.' if demo else 'Ukończenie potwierdza ręczne wysłanie fizycznego zamówienia. Zwrot przywróci saldo i stan magazynowy.'
            await render(callback, title('⚠️ POTWIERDZENIE ZAMÓWIENIA') + f'ID: <code>{safe(data[2])}</code>\nNowy status: <b>{safe(data[3])}</b>\n\n' + confirmation, keyboard([[('✅ Potwierdź', f'a:orun:{data[2]}:{data[3]}')]], back='a:menu'))
        elif action == 'orun':
            order = await service.admin_order(callback.from_user.id, data[2], data[3])
            target = await service.telegram_for_user(order.user_id)
            text = f'📦 Zamówienie <code>{order.id}</code>\nStatus: <b>{safe(order.status)}</b>'
            if order.demo_code and order.status == 'completed':
                text += f'\n\nFikcyjny kod DEMO: <code>{safe(order.demo_code)}</code>'
            if order.status in ('cancelled', 'refunded'):
                text += f'\nPrzywrócono saldo: {money(order.price_grosze)}.'
            sent = await notify(callback.bot, target, text)
            await show_order(callback, order.id)
            if not sent:
                await callback.message.answer('Status zapisano. Telegram nie dostarczył powiadomienia; użytkownik zobaczy zmianę w „Moje zamówienia”.')
        elif action == 'topups':
            page = max(0, int(data[2]))
            topups = await service.admin_topups(callback.from_user.id)
            topups = sorted(topups, key=lambda t: (t.status not in ('pending', 'awaiting_quote', 'awaiting_payment', 'submitted'), -t.created_at.timestamp()))
            rows = [[(f'{t.id[:8]} · {money(t.amount_grosze)} · {t.status}', f'a:topup:{t.id}')] for t in topups[page * 8:page * 8 + 8]]
            if page:
                rows.append([('⬅️ Poprzednie', f'a:topups:{page - 1}')])
            if len(topups) > (page + 1) * 8:
                rows.append([('➡️ Następne', f'a:topups:{page + 1}')])
            await render(callback, title('💳 DOŁADOWANIA' + suffix) + 'Najpierw oczekujące zgłoszenia.', keyboard(rows, back='a:menu'))
        elif action == 'topup':
            await show_topup(callback, data[2])
        elif action == 'tquote' and not demo:
            if settings.crypto_auto_rates:
                await state.clear()
                raise ValueError('Wyceny krypto są obliczane automatycznie. Ręczne nadpisywanie jest wyłączone.')
            topup = next((t for t in await service.admin_topups(callback.from_user.id) if t.id == data[2]), None)
            if topup is None or topup.method != 'crypto' or topup.status not in ('awaiting_quote', 'awaiting_payment'):
                raise ValueError('Dla tego zgłoszenia nie można ustalić wyceny.')
            await ask(callback, state, 'topup_quote', f'Do uznania: <b>{money(topup.amount_grosze)}</b>\nWaluta: <b>{safe(topup.asset)}</b>\nSieć: {safe(topup.network)}\n\nPodaj dokładną ilość kryptowaluty, którą ma otrzymać właściciel, np. 0.005. Nie wpisuj kwoty PLN ani symbolu waluty. Wycena ważna 30 minut; uwzględnij kurs i opłaty.', topup_id=topup.id)
        elif action == 'tapprove' and not demo:
            topup = next((t for t in await service.admin_topups(callback.from_user.id) if t.id == data[2]), None)
            if topup is None or topup.status != 'submitted':
                raise ValueError('Najpierw użytkownik musi zgłosić wykonanie wpłaty.')
            reference_help = 'Podaj hash transakcji (64 znaki szesnastkowe; ETH/USDC także z prefiksem 0x).' if topup.method == 'crypto' else 'Podaj unikalny numer potwierdzenia rozliczenia BLIK, nie kod BLIK.'
            if topup.asset == 'SOL':
                reference_help = 'Podaj podpis transakcji Solana (base58).'
            await ask(callback, state, 'topup_approve', f'Użytkownik zgłosił wpłatę na <b>{money(topup.amount_grosze)}</b> salda PLN.\n\n<b>Najpierw samodzielnie sprawdź wpływ środków.</b> {reference_help} Ten formularz nie wykonuje ani nie sprawdza przelewu.', topup_id=topup.id)
        elif action == 'tconf':
            if data[3] not in ('yes', 'no'):
                raise ValueError('Nieprawidłowa decyzja.')
            if not demo and data[3] == 'yes':
                raise ValueError('Użyj formularza ręcznej weryfikacji wpłaty i podaj referencję potwierdzenia.')
            await render(callback, title('⚠️ POTWIERDZENIE DOŁADOWANIA') + f'ID: <code>{safe(data[2])}</code>\nDecyzja: {"zatwierdź testowe saldo" if data[3] == "yes" else "odrzuć"}.', keyboard([[('✅ Potwierdź', f'a:trun:{data[2]}:{data[3]}')]], back='a:menu'))
        elif action == 'trun':
            if data[3] not in ('yes', 'no'):
                raise ValueError('Nieprawidłowa decyzja.')
            if not demo and data[3] == 'yes':
                raise ValueError('Użyj formularza ręcznej weryfikacji wpłaty i podaj referencję potwierdzenia.')
            topup = await service.admin_topup(callback.from_user.id, data[2], data[3] == 'yes')
            target = await service.telegram_for_user(topup.user_id)
            await notify(callback.bot, target, f'💳 Zgłoszenie{suffix} <code>{topup.id}</code>\nKwota: {money(topup.amount_grosze)}\nStatus: <b>{safe(topup.status)}</b>', keyboard([[('💳 Sprawdź status', f'topup_status:{topup.id}')]]))
            await show_topup(callback, topup.id)
        elif action == 'stats':
            stats = await service.admin_stats(callback.from_user.id)
            labels = [('users', 'Użytkownicy'), ('orders', 'Zamówienia'), ('completed', 'Zakończone'), ('turnover_grosze', 'Obrót' + suffix), ('referrals', 'Skuteczne polecenia'), ('pending_topups', 'Oczekujące doładowania'), ('pending_orders', 'Zamówienia w kolejce')]
            text = title('📊 STATYSTYKI') + '\n'.join(f'{label}: {money(stats[key]) if key.endswith("grosze") else stats[key]}' for key, label in labels)
            if not demo:
                text += '\n\n' + await rate_details()
            await render(callback, text, keyboard([], back='a:menu'))
        elif action == 'broadcast':
            await ask(callback, state, 'broadcast', 'Podaj treść powiadomienia tekstowego (maks. 3000 znaków). Przed wysyłką zobaczysz treść i liczbę odbiorców. Formatowanie HTML zostanie potraktowane jako zwykły tekst.')
        elif action == 'settings':
            support = await service.setting('support_text', 'Skontaktuj się z administratorem sklepu.')
            if demo:
                details = f'Tryb: DEMO\nBonus polecenia: {money(settings.referral_bonus_grosze)}'
            else:
                configured = ', '.join(o['code'] for o in service.crypto_options() if o['configured']) or 'brak'
                contact = '@' + settings.merchant_telegram_username if settings.merchant_telegram_username else 'nie ustawiono'
                details = f'Tryb: rzeczywiste salda, ręczne wpłaty\nKontakt BLIK: {safe(contact)}\nKrypto z adresem: {safe(configured)}\nPremie pieniężne za polecenia: wyłączone'
                details += '\n\n' + await rate_details()
            await render(callback, title('⚙️ USTAWIENIA') + details + f'\n\nTekst pomocy:\n{safe(support)}\n\nKontakt i adresy zmienisz w .env i zastosujesz przez restart. Bot nie potwierdza automatycznie wpłat.', keyboard([[('✍️ Edytuj pomoc', 'a:support')]], back='a:menu'))
        elif action == 'support':
            await ask(callback, state, 'support', 'Podaj nowy tekst pomocy (maks. 2000 znaków).')
        elif action == 'execute':
            context = await state.get_data()
            if await state.get_state() != AdminInput.confirmation.state or context.get('token') != data[2] or context.get('expires_at', 0) <= time.monotonic():
                await state.clear()
                raise ValueError('Potwierdzenie wygasło lub zostało już użyte. Otwórz /admin i ponów formularz.')
            op, payload = context['operation'], context['payload']
            # Single-process FSM event isolation serializes confirmations; clear before mutation.
            await state.clear()
            if op == 'topup_quote' and not demo:
                if settings.crypto_auto_rates:
                    raise ValueError('Wyceny krypto są obliczane automatycznie. Ręczne nadpisywanie jest wyłączone.')
                topup = await service.admin_quote_topup(callback.from_user.id, payload['topup_id'], payload['crypto_amount'])
                target = await service.telegram_for_user(topup.user_id)
                await notify(callback.bot, target, f'💱 Wycena wpłaty MONSTAL\nSaldo do uznania: {money(topup.amount_grosze)}\nKwota: <b>{safe(topup.crypto_amount)} {safe(topup.asset)}</b>\nSieć: {safe(topup.network)}\nOtwórz szczegóły, sprawdź adres i termin ważności przed wysłaniem.', keyboard([[('💳 Szczegóły wpłaty', f'topup_status:{topup.id}')]]))
                await show_topup(callback, topup.id)
            elif op == 'topup_approve' and not demo:
                topup = await service.admin_topup(callback.from_user.id, payload['topup_id'], True, confirmation_reference=payload['reference'])
                target = await service.telegram_for_user(topup.user_id)
                await notify(callback.bot, target, f'✅ Administrator potwierdził wpłatę MONSTAL\nZgłoszenie: <code>{topup.id}</code>\nUznano saldo PLN: <b>{money(topup.amount_grosze)}</b>', keyboard([[('👤 Mój profil', 'profile')]]))
                await show_topup(callback, topup.id)
            elif op == 'balance':
                user = await service.admin_balance(callback.from_user.id, payload['target'], payload['delta'], payload['request_key'])
                await notify(callback.bot, user.telegram_id, f'💰 Administrator skorygował Twoje saldo{suffix} o {money(payload["delta"])}.\nNowe saldo: {money(user.balance_grosze)}.')
                await show_user(callback, user.telegram_id)
            elif op == 'category':
                category = await service.admin_category(callback.from_user.id, payload['name'])
                await render(callback, title('✅ KATEGORIA ZAPISANA') + f'#{category.id} · {safe(category.name)}', keyboard([], back='a:menu'))
            elif op in ('product_new', 'product_edit'):
                product = await service.admin_product(callback.from_user.id, payload.get('product_id'), **payload['fields'])
                await render(callback, title('✅ PRODUKT ZAPISANY') + f'#{product.id} · {safe(product.name)}', keyboard([[('🎁 Otwórz produkt', f'a:product:{product.id}')]], back='a:menu'))
            elif op == 'product_delete':
                await service.admin_delete_product(callback.from_user.id, payload['product_id'])
                await render(callback, title('✅ PRODUKT USUNIĘTY'), keyboard([], back='a:menu'))
            elif op == 'status':
                await service.admin_set_user_status(callback.from_user.id, payload['target'], payload['status'])
                await show_user(callback, payload['target'])
            elif op == 'support':
                await service.admin_setting(callback.from_user.id, 'support_text', payload['value'])
                await render(callback, title('✅ TEKST POMOCY ZAPISANY'), keyboard([], back='a:menu'))
            elif op == 'broadcast':
                recipients = await service.broadcast_recipients(callback.from_user.id)
                await service.audit(callback.from_user.id, 'broadcast_started', 'all_active_users', f'recipients={len(recipients)}')
                await callback.answer('Rozpoczynam wysyłkę.')
                sent = 0
                for target in recipients:
                    sent += await notify(callback.bot, target, title('📢 MONSTAL — POWIADOMIENIE') + safe(payload['text']))
                    await asyncio.sleep(0.05)
                await service.audit(callback.from_user.id, 'broadcast_completed', 'all_active_users', f'sent={sent};failed={len(recipients)-sent}')
                await render(callback, title('📢 WYSYŁKA ZAKOŃCZONA') + f'Dostarczono: {sent}\nNiedostarczono: {len(recipients) - sent}\nWysyłka nie jest automatycznie wznawiana po restarcie.', keyboard([], back='a:menu'))
                return
            else:
                raise ValueError('Nieznana operacja.')
        else:
            raise ValueError('Nieaktualny przycisk. Otwórz /admin.')
        await callback.answer()

    return router
