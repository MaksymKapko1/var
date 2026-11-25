"""Обёртка над официальным SDK `variational`.

Variational — RFQ-биржа: нет стакана и лимитных ордеров. Сделка =
  1) create_rfq  (запрос котировки у маркет-мейкеров),
  2) ждём котировки (bids/asks) в get_rfqs_sent,
  3) accept_quote лучшей котировки,
  4) если нужен депозит маржи — подписываем permit кошельком (WALLET_PRIVATE_KEY),
  5) ждём clearing_status == success_trades_booked_into_pool.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from variational import Client, ClearingStatus, PollingHelper, TransferPermitHelper

from .config import Config

log = logging.getLogger("exchange")


class TradeFailed(Exception):
    pass


class Exchange:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = Client(cfg.api_key, cfg.api_secret, base_url=cfg.base_url, request_timeout=15)
        self.poller = PollingHelper(self.client, interval=1, attempts=60)
        self.permit = TransferPermitHelper(self.client, cfg.wallet_private_key) if cfg.wallet_private_key else None
        self._lp_companies: list[str] | None = None

    # ---------- info ----------
    def me(self) -> dict:
        return self.client.get_me().result

    def summary(self) -> dict:
        return self.client.get_portfolio_summary().result

    def limits(self) -> dict:
        return self.client.get_limits().result

    def mark_price(self) -> Decimal:
        res = self.client.price_instrument(self.cfg.instrument).result
        return Decimal(res["price"])

    def position_qty(self) -> Decimal:
        total = Decimal(0)
        for p in self.client.get_portfolio_positions().result:
            inst = p["instrument"]
            if inst.get("underlying") == self.cfg.underlying and inst.get("instrument_type") == self.cfg.instrument_type:
                total += Decimal(p["qty"])
        return total

    def lp_companies(self) -> list[str]:
        if self._lp_companies is None:
            comps = self.client.get_companies().result
            self._lp_companies = [c["id"] for c in comps if c.get("is_lp")]
            log.info("Маркет-мейкеров (LP) найдено: %d", len(self._lp_companies))
        return self._lp_companies

    # ---------- trading ----------
    def market_trade(self, side: str, qty: Decimal, ref_price: Decimal) -> Decimal:
        """Исполняет сделку через RFQ. Возвращает цену исполнения."""
        expires = (datetime.now(timezone.utc) + timedelta(seconds=self.cfg.rfq_ttl_sec)).isoformat().replace("+00:00", "Z")
        structure = {"legs": [{"side": "buy", "ratio": 1, "instrument": self.cfg.instrument}]}
        rfq = self.client.create_rfq(
            structure=structure, qty=str(qty), expires_at=expires, target_companies=self.lp_companies()
        ).result
        rfq_id = rfq["rfq_id"]
        log.info("RFQ %s создан (%s %s %s)", rfq_id, side, qty, self.cfg.underlying)

        try:
            quote_id, price = self._wait_best_quote(rfq_id, side)
        except TradeFailed:
            self._safe_cancel(rfq_id)
            raise

        slippage = abs(price - ref_price) / ref_price * 100
        if slippage > self.cfg.max_slippage_pct:
            self._safe_cancel(rfq_id)
            raise TradeFailed(f"котировка {price} хуже mark {ref_price} на {slippage:.3f}% > MAX_SLIPPAGE_PCT")

        acc = self.client.accept_quote(rfq_id=rfq_id, parent_quote_id=quote_id, side=side).result
        log.info("Котировка %s принята по %s, статус: %s", quote_id, price, acc["new_clearing_status"])

        if acc["new_clearing_status"] == ClearingStatus.PENDING_TAKER_DEPOSIT_APPROVAL:
            self._approve_deposit(acc)

        quote = self.poller.wait_for_clearing_status(quote_id, ClearingStatus.SUCCESS_TRADES_BOOKED_INTO_POOL)
        log.info("Сделка проведена: %s", quote["clearing_status"])
        return price

    def _wait_best_quote(self, rfq_id: str, side: str) -> tuple[str, Decimal]:
        deadline = time.time() + self.cfg.quote_wait_sec
        while time.time() < deadline:
            rfqs = self.client.get_rfqs_sent(id=rfq_id).result
            if rfqs:
                book = rfqs[0]["asks"] if side == "buy" else rfqs[0]["bids"]
                if book:
                    pick = min if side == "buy" else max
                    best = pick(book, key=lambda q: Decimal(q["quote_price"]))
                    return best["parent_quote_id"], Decimal(best["quote_price"])
            time.sleep(1)
        raise TradeFailed(f"нет котировок на RFQ {rfq_id} за {self.cfg.quote_wait_sec}s")

    def _approve_deposit(self, acc: dict):
        pool = acc.get("pending_settlement_pool") or {}
        pool_addr = pool.get("address")
        if not self.permit:
            raise TradeFailed("требуется депозит маржи в пул, но WALLET_PRIVATE_KEY не задан")
        if not pool_addr:
            pool = self.poller.wait_for_settlement_pool(pool["id"])
            pool_addr = pool["data"]["address"]
        amount = acc["pending_deposits_sum_qty"]
        log.info("Подписываю permit на депозит %s в пул %s", amount, pool_addr)
        self.permit.sign_and_submit_decimal(pool_addr, amount)

    def _safe_cancel(self, rfq_id: str):
        try:
            self.client.cancel_rfq(rfq_id)
        except Exception as e:  # noqa: BLE001
            log.debug("cancel_rfq %s: %s", rfq_id, e)
