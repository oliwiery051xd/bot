from decimal import Decimal, InvalidOperation
from html import escape


def safe(value: object) -> str:
    return escape(str(value))


def money(grosze: int) -> str:
    sign = '-' if grosze < 0 else ''
    value = abs(grosze)
    return f'{sign}{value // 100},{value % 100:02d} zł'


def amount(text: str, *, signed: bool = False) -> int:
    try:
        value = Decimal(text.strip().replace(',', '.'))
        if not value.is_finite() or value.as_tuple().exponent < -2 or abs(value) > 1_000_000:
            raise ValueError
        result = int(value * 100)
        if result == 0 or abs(result) > 100_000_000 or (not signed and result < 0):
            raise ValueError
        return result
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError('Podaj kwotę w złotych, np. 25,50 (maksymalnie dwa miejsca po przecinku).') from None


def title(label: str) -> str:
    return f'<b>{safe(label)}</b>\n━━━━━━━━━━━━━━━━\n\n'
