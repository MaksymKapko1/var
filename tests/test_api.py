from decimal import Decimal as D

from gridbot.api import canonical_string, load_private_key, public_key_hex
from gridbot.precision import fmt, round_price, round_size

SEED = "0x" + "33" * 32
PUB = "0x17cb79fb2b4120f2b1ec65e4198d6e08b28e813feb01e4a400839b85e18080ce"


def test_signing_vector_from_docs():
    # вектор из https://docs.variational.io/api-specification/authentication
    k = load_private_key(SEED)
    assert public_key_hex(k) == PUB
    c = canonical_string("test", PUB, 1790000000123, "GET", "/v1/orders?limit=10&symbol=BTC-PERP", b"")
    assert c.splitlines()[0] == "variational-omni-api/v1"
    assert "0x" + k.sign(c.encode()).hex() == (
        "0x9f0fd3388c95fcd9ee6e581a03a4757fbd4aeec8cb103731b98e9800e5f9e73a"
        "e9c35db7ce6d8df1918a6a3257dd308999f3c695af1670b7d21372b7bf3b600b")


PREC = {"min_decimal_figures": 2, "max_decimal_only_figures": 4, "max_significant_figures": 6}


def test_price_precision_table_from_docs():
    assert round_price(D("123456.78"), PREC) == D("123456.78")
    assert round_price(D("123.456"), PREC) == D("123.456")
    assert round_price(D("123.4567"), PREC) == D("123.457")
    assert round_price(D("0.001234"), PREC) == D("0.001234")
    assert round_price(D("0.0012345"), PREC) == D("0.001235")
    assert round_price(D("3333.333333"), PREC) == D("3333.33")


def test_size_and_fmt():
    assert round_size(D("0.0137"), D("0.001")) == D("0.013")
    assert fmt(D("1E+3")) == "1000"
    assert fmt(D("0.00010")) == "0.0001"


def test_client_signs_exact_bytes_sent():
    import hashlib
    from unittest.mock import MagicMock
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from gridbot.api import OmniClient

    c = OmniClient("https://api.omni.testnet.variational.io", "testnet", PUB, SEED)
    resp = MagicMock(status_code=202, content=b"{}", headers={"X-RateLimit-Remaining": "50"})
    resp.json.return_value = {"placed": [{"order_id": "1"}], "results": []}
    c.sesh.request = MagicMock(return_value=resp)
    c.place_order({"type": "limit", "symbol": "ETH-PERP", "client_order_id": "a:b-1"})
    c.open_orders("ETH-PERP")
    for call in c.sesh.request.call_args_list:
        method, url = call.args
        h = call.kwargs["headers"]
        body = call.kwargs["data"] or b""
        path = url.removeprefix("https://api.omni.testnet.variational.io")
        msg = "\n".join(["variational-omni-api/v1", "testnet", PUB, h["x-request-timestamp-ms"], method, path,
                         hashlib.sha256(body).hexdigest()])
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUB[2:])).verify(
            bytes.fromhex(h["x-variational-signature"][2:]), msg.encode())
        if body:
            assert h["content-type"] == "application/json"
    assert c.sesh.request.call_args_list[1].args[1].endswith("/v1/orders?symbol=ETH-PERP")
    assert c.rl_remaining == 50
