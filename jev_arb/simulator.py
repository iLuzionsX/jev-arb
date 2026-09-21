from __future__ import annotations

from collections.abc import Callable

from .config import AppConfig
from .domain import Fill, Opportunity, TradeResult
from .inventory import InventoryBook
from .orderbook import OrderBook, walk_levels


BookProvider = Callable[[str, str, int], OrderBook | None]


class ExecutionSimulator:
    def __init__(self, config: AppConfig, book_provider: BookProvider):
        self.config = config
        self.book_provider = book_provider

    def simulate(
        self,
        candidate: Opportunity,
        strategy: str,
        accepted: bool,
        decision_at_ms: int,
        arrival_at_ms: int,
        portfolio: InventoryBook,
        mutate: bool,
    ) -> TradeResult:
        result = TradeResult(
            candidate_id=candidate.candidate_id,
            strategy=strategy,
            accepted=accepted,
            detection_at_ms=candidate.detected_at_ms,
            decision_at_ms=decision_at_ms,
            arrival_at_ms=arrival_at_ms,
            expected_pnl_usd=candidate.expected_profit_usd,
        )
        buy_book = self.book_provider(candidate.buy_venue, candidate.symbol, arrival_at_ms)
        sell_book = self.book_provider(candidate.sell_venue, candidate.symbol, arrival_at_ms)
        if not buy_book or not sell_book or not buy_book.snapshot_ready or not sell_book.snapshot_ready:
            result.status = "missed_opportunity"
            result.notes.append("no_valid_order_book_at_simulated_arrival")
            result.expected_vs_realized_delta_usd = result.expected_pnl_usd
            return result
        inventory_ok, inventory_reasons = portfolio.can_execute(candidate, self.config.venues()[candidate.buy_venue].taker_fee_pct)
        if not inventory_ok:
            result.status = "inventory_blocked"
            result.notes.extend(inventory_reasons)
            result.expected_vs_realized_delta_usd = result.expected_pnl_usd
            return result
        buy_walk = walk_levels(buy_book.ask_levels(25), candidate.quantity)
        sell_walk = walk_levels(sell_book.bid_levels(25), candidate.quantity)
        buy_rate = self.config.quote_to_usd.get(candidate.buy_quote, 1.0)
        sell_rate = self.config.quote_to_usd.get(candidate.sell_quote, 1.0)
        buy_fee_usd = buy_walk.quote_amount * buy_rate * self.config.venues()[candidate.buy_venue].taker_fee_pct
        sell_fee_usd = sell_walk.quote_amount * sell_rate * self.config.venues()[candidate.sell_venue].taker_fee_pct
        result.filled_buy_qty = buy_walk.filled_qty
        result.filled_sell_qty = sell_walk.filled_qty
        result.buy_fill = Fill(
            venue=candidate.buy_venue,
            side="buy",
            base_qty=buy_walk.filled_qty,
            quote_amount=buy_walk.quote_amount,
            vwap=buy_walk.vwap,
            fee_usd=buy_fee_usd,
            quote=candidate.buy_quote,
        )
        result.sell_fill = Fill(
            venue=candidate.sell_venue,
            side="sell",
            base_qty=sell_walk.filled_qty,
            quote_amount=sell_walk.quote_amount,
            vwap=sell_walk.vwap,
            fee_usd=sell_fee_usd,
            quote=candidate.sell_quote,
        )
        result.net_cashflow_usd = sell_walk.quote_amount * sell_rate - buy_walk.quote_amount * buy_rate - buy_fee_usd - sell_fee_usd
        both_legs = buy_walk.filled_qty > 0 and sell_walk.filled_qty > 0
        fully_filled = buy_walk.complete and sell_walk.complete
        if not both_legs:
            result.status = "missed_opportunity" if buy_walk.filled_qty == 0 and sell_walk.filled_qty == 0 else "one_leg_fill"
            result.notes.append("one_leg_or_zero_fill_risk_realized")
            result.mark_to_market_pnl_usd = self._mark_residual(candidate, buy_book, sell_book, buy_walk.filled_qty, sell_walk.filled_qty, buy_walk.quote_amount * buy_rate, sell_walk.quote_amount * sell_rate, buy_rate, sell_rate)
            result.realized_pnl_usd = 0.0
        else:
            result.status = "executed" if fully_filled else "partial_fill"
            matched_fraction = min(1.0, buy_walk.filled_qty / max(candidate.quantity, 1e-12), sell_walk.filled_qty / max(candidate.quantity, 1e-12))
            rebalance = candidate.rebalance_cost_estimate_usd * matched_fraction
            result.realized_pnl_usd = result.net_cashflow_usd - rebalance
            result.mark_to_market_pnl_usd = self._mark_residual(candidate, buy_book, sell_book, buy_walk.filled_qty, sell_walk.filled_qty, buy_walk.quote_amount * buy_rate, sell_walk.quote_amount * sell_rate, buy_rate, sell_rate)
        result.expected_vs_realized_delta_usd = candidate.expected_profit_usd - result.realized_pnl_usd
        if mutate:
            if result.buy_fill and result.buy_fill.base_qty > 0:
                portfolio.apply_buy(candidate.buy_venue, candidate.base, candidate.buy_quote, result.buy_fill.base_qty, result.buy_fill.quote_amount, result.buy_fill.fee_usd, buy_rate)
            if result.sell_fill and result.sell_fill.base_qty > 0:
                portfolio.apply_sell(candidate.sell_venue, candidate.base, candidate.sell_quote, result.sell_fill.base_qty, result.sell_fill.quote_amount, result.sell_fill.fee_usd, sell_rate)
        return result

    def _mark_residual(self, candidate: Opportunity, buy_book: OrderBook, sell_book: OrderBook, buy_qty: float, sell_qty: float, buy_cost_usd: float, sell_proceeds_usd: float, buy_rate: float, sell_rate: float) -> float:
        residual_buy = max(0.0, buy_qty - sell_qty)
        residual_sell = max(0.0, sell_qty - buy_qty)
        buy_mid = (buy_book.mid_price() or candidate.buy_best_price) * buy_rate
        sell_mid = (sell_book.mid_price() or candidate.sell_best_price) * sell_rate
        if residual_buy > 0:
            average_cost = buy_cost_usd / buy_qty if buy_qty else 0.0
            return residual_buy * buy_mid - residual_buy * average_cost
        if residual_sell > 0:
            average_proceeds = sell_proceeds_usd / sell_qty if sell_qty else 0.0
            return residual_sell * average_proceeds - residual_sell * sell_mid
        return 0.0

