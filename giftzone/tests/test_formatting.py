import pytest

from app.utils.formatting import amount


@pytest.mark.parametrize("text", ["NaN", "Infinity", "1e999999", "0.001", "-1", "0"])
def test_invalid_amount_never_converts_unbounded_integer(text):
    with pytest.raises(ValueError):
        amount(text)


def test_amount_uses_exact_grosze_and_signed_corrections():
    assert amount("25,50") == 2550
    assert amount("-0.01", signed=True) == -1
