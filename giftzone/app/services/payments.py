"""Payment boundary. DEMO provider and future verified integration boundary; manual credits live in ShopService."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PaymentIntent:
    external_id: str
    amount_grosze: int
    method: str
    network: str
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class VerifiedPaymentEvent:
    """Future providers must return a verified, uniquely identified event."""
    event_id: str
    external_id: str
    amount_grosze: int
    status: str


class PaymentProvider(Protocol):
    async def create_deposit(self, amount_grosze: int, method: str, deposit_id: str) -> PaymentIntent: ...

    async def verify_webhook(self, body: bytes, signature: str) -> VerifiedPaymentEvent: ...


class DemoPaymentProvider:
    """No bank, wallet, network transfers or payment webhook are contacted."""

    async def create_deposit(self, amount_grosze: int, method: str, deposit_id: str) -> PaymentIntent:
        if method not in {"blik", "crypto"} or amount_grosze <= 0:
            raise ValueError("Nieprawidłowa symulacja wpłaty.")
        return PaymentIntent(
            external_id=f"FICTIONAL-DEMO-{deposit_id}", amount_grosze=amount_grosze,
            method=method, network="MONSTAL-SIMULATION (nie jest siecią blockchain)",
        )

    async def verify_webhook(self, body: bytes, signature: str) -> VerifiedPaymentEvent:
        raise NotImplementedError(
            "Webhooki płatnicze nie są wdrożone. DEMO zatwierdza wyłącznie administrator."
        )
