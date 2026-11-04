"""Fixed manual-payment destinations. No wallet generation, transfers or rates."""
from decimal import Decimal, InvalidOperation
import hashlib
import re

CRYPTO_ASSETS = {
    "BTC": {"name": "Bitcoin", "network": "Bitcoin mainnet", "decimals": 8, "setting": "crypto_btc_address"},
    "ETH": {"name": "Ethereum", "network": "Ethereum mainnet", "decimals": 18, "setting": "crypto_eth_address"},
    "USDC": {"name": "USD Coin", "network": "Ethereum mainnet · ERC-20", "decimals": 6, "setting": "crypto_usdc_address"},
    "SOL": {"name": "Solana", "network": "Solana mainnet", "decimals": 9, "setting": "crypto_sol_address"},
    "LTC": {"name": "Litecoin", "network": "Litecoin mainnet", "decimals": 8, "setting": "crypto_ltc_address"},
    "XMR": {"name": "Monero", "network": "Monero mainnet", "decimals": 12, "setting": "crypto_xmr_address"},
}

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(value):
    result = 0
    for character in value:
        result = result * 58 + BASE58.index(character)
    return b"\0" * (len(value) - len(value.lstrip("1"))) + result.to_bytes((result.bit_length() + 7) // 8, "big")


def validate_address(asset: str, address: str) -> None:
    """Validate recognizable network syntax, not wallet ownership/existence.

    Address ownership and the exact selected mainnet must still be verified by
    the merchant. We do not claim to verify blockchain state or EIP-55/Monero
    checksums. Bitcoin/Litecoin legacy Base58 checksums are checked here.
    """
    valid = False
    if asset in {"ETH", "USDC"}:
        valid = re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is not None and int(address[2:], 16) != 0
    elif asset == "SOL":
        if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address):
            valid = len(_base58_decode(address)) == 32
    elif asset == "XMR":
        valid = re.fullmatch(r"[48][1-9A-HJ-NP-Za-km-z]{94}|4[1-9A-HJ-NP-Za-km-z]{105}", address) is not None
    elif asset in {"BTC", "LTC"}:
        prefix = "bc1" if asset == "BTC" else "ltc1"
        # Bech32 syntax and network prefix; ownership and checksum verification
        # remain a merchant prerequisite before enabling a destination.
        valid = re.fullmatch(prefix + r"[023456789acdefghjklmnpqrstuvwxyz]{11,87}", address) is not None
        if not valid and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{26,35}", address):
            decoded = _base58_decode(address)
            versions = {0, 5} if asset == "BTC" else {48, 50}
            valid = len(decoded) == 25 and decoded[0] in versions and hashlib.sha256(hashlib.sha256(decoded[:-4]).digest()).digest()[:4] == decoded[-4:]
    if not valid:
        raise ValueError(f"Nieprawidłowy format adresu {asset} dla wybranej sieci mainnet.")


def crypto_amount(asset: str, raw: str) -> str:
    if asset not in CRYPTO_ASSETS or not isinstance(raw, str) or not re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", raw.strip()) or len(raw) > 80:
        raise ValueError("Podaj dodatnią kwotę kryptowaluty bez notacji naukowej.")
    try:
        value = Decimal(raw.strip().replace(",", "."))
        if not value.is_finite() or not 0 < value <= Decimal("1000000000") or value.as_tuple().exponent < -CRYPTO_ASSETS[asset]["decimals"]:
            raise ValueError("Nieprawidłowa kwota lub zbyt wiele miejsc po przecinku.")
        return format(value, "f")
    except InvalidOperation:
        raise ValueError("Nieprawidłowa kwota kryptowaluty.") from None
