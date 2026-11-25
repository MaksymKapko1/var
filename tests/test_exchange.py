from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from gridbot.config import Config
from gridbot.exchange import Exchange, TradeFailed


def cfg(**kw):
    base = dict(api_key="k", api_secret="00", base_url="x", wallet_private_key="", underlying="ETH",
                instrument_type="perpetual_future", settlement_asset="USDC", grid_lower=D(1), grid_upper=D(2),
                grid_levels=3, order_qty=D("0.1"), poll_interval=0, rfq_ttl_sec=30, quote_wait_sec=1,
                max_slippage_pct=D("0.5"), dry_run=False, state_file="", log_level="INFO")
    base.update(kw)
    return Config(**base)


def make_ex(c, status="success_trades_booked_into_pool"):
    ex = Exchange(c)
    cl = MagicMock()
    cl.get_companies.return_value = NS(result=[{"id": "lp1", "is_lp": True}, {"id": "me", "is_lp": False}])
    cl.create_rfq.return_value = NS(result={"rfq_id": "r1"})
    cl.get_rfqs_sent.return_value = NS(result=[{
        "asks": [{"parent_quote_id": "qa1", "quote_price": "2001"}, {"parent_quote_id": "qa2", "quote_price": "2000.5"}],
        "bids": [{"parent_quote_id": "qb1", "quote_price": "1999"}]}])
    cl.accept_quote.return_value = NS(result={"new_clearing_status": status, "pending_deposits_sum_qty": "10",
                                              "pending_settlement_pool": {"id": "p", "address": "0xabc"}})
    ex.client = cl
    ex.poller = MagicMock()
    ex.poller.wait_for_clearing_status.return_value = {"clearing_status": "success_trades_booked_into_pool"}
    return ex, cl


def test_buy_picks_best_ask():
    ex, cl = make_ex(cfg())
    assert ex.market_trade("buy", D("0.1"), D(2000)) == D("2000.5")
    assert cl.create_rfq.call_args.kwargs["target_companies"] == ["lp1"]
    cl.accept_quote.assert_called_with(rfq_id="r1", parent_quote_id="qa2", side="buy")


def test_sell_picks_best_bid():
    ex, cl = make_ex(cfg())
    assert ex.market_trade("sell", D("0.1"), D(2000)) == D(1999)


def test_slippage_guard_cancels():
    ex, cl = make_ex(cfg(max_slippage_pct=D("0.01")))
    with pytest.raises(TradeFailed):
        ex.market_trade("buy", D("0.1"), D(2000))
    cl.cancel_rfq.assert_called_with("r1")
    cl.accept_quote.assert_not_called()


def test_deposit_needs_wallet():
    ex, _ = make_ex(cfg(), status="pending_taker_deposit_approval")
    with pytest.raises(TradeFailed):
        ex.market_trade("buy", D("0.1"), D(2000))


def test_deposit_signed_with_wallet():
    ex, _ = make_ex(cfg(), status="pending_taker_deposit_approval")
    ex.permit = MagicMock()
    ex.market_trade("buy", D("0.1"), D(2000))
    ex.permit.sign_and_submit_decimal.assert_called_with("0xabc", "10")
