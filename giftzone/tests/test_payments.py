import pytest

from app.services.payments import DemoPaymentProvider


@pytest.mark.asyncio
async def test_demo_payment_intent_has_explicitly_fictional_network():
    intent = await DemoPaymentProvider().create_deposit(1000, "crypto", "test-deposit")
    assert intent.external_id == "FICTIONAL-DEMO-test-deposit"
    assert intent.amount_grosze == 1000
    assert "SIMULATION" in intent.network
    assert intent.status == "pending"


@pytest.mark.asyncio
async def test_demo_provider_never_accepts_payment_webhooks():
    with pytest.raises(NotImplementedError, match="nie są wdrożone"):
        await DemoPaymentProvider().verify_webhook(b"{}", "untrusted")
