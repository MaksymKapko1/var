from decimal import Decimal as D

import pytest

from gridbot.exchange.signer import Signer, canonical_string
from gridbot.strategy.precision import fmt, round_price, round_size

SEED = "0x" + "33" * 32
PUB = "0x17cb79fb2b4120f2b1ec65e4198d6e08b28e813feb01e4a400839b85e18080ce"


def test_signing_vector_from_docs():
    s = Signer(SEED, "test")
    assert s.public_key == PUB
    h = s.headers(1790000000123, "GET", "/v1/orders?limit=10&symbol=BTC-PERP", b"")
    assert h["x-variational-signature"] == (
        "0x9f0fd3388c95fcd9ee6e581a03a4757fbd4aeec8cb103731b98e9800e5f9e73a"
        "e9c35db7ce6d8df1918a6a3257dd308999f3c695af1670b7d21372b7bf3b600b")
    assert canonical_string("t", PUB, 1, "get", "/x", b"").split("\n")[4] == "GET"


def test_signer_rejects_mismatched_public_key():
    with pytest.raises(ValueError):
        Signer(SEED, "testnet", "0x" + "00" * 32)
    with pytest.raises(ValueError):
        Signer("abcd", "testnet")


PREC = {"min_decimal_figures": 2, "max_decimal_only_figures": 4, "max_significant_figures": 6}


@pytest.mark.parametrize("raw,exp", [("123456.78", "123456.78"), ("123.456", "123.456"), ("123.4567", "123.457"),
                                     ("0.001234", "0.001234"), ("0.0012345", "0.001235"), ("1683.333333", "1683.33")])
def test_price_precision_table_from_docs(raw, exp):
    assert round_price(D(raw), PREC) == D(exp)


def test_size_and_fmt():
    assert round_size(D("0.0137"), D("0.001")) == D("0.013")
    assert fmt(D("1E+3")) == "1000"
