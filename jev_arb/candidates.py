from __future__ import annotations

import collections
import itertools
import math
from dataclasses import dataclass

from .config import AppConfig, MarketSpec
from .domain import Opportunity
from .inventory import InventoryBook
from .orderbook import BookStore, OrderBook, quantize_down, walk_levels


@dataclass
class SpreadState:
    first_seen_ms: int
    last_seen_ms: int
    last_emitted_ms: int = 0
    last_spread_pct: float = 0.0


class CandidateDetector:
    def __init__(self, config: AppConfig, books: BookStore, run_id: str, reference_inventory: InventoryBook):
        self.config = config
        self.books = books
        self.run_id = run_id
        self.reference_inventory = reference_inventory
        self._spread_history: dict[tuple[str, str, str], collections.deque[tuple[int, float]]] = {}
        self._mid_history: dict[tuple[str, str], collections.deque[tuple[int, float]]] = {}
        self._active: dict[tuple[str, str, str], SpreadState] = {}

    def detect(self, at_ms: int) -> list[Opportunity]:
        candidates: list[Opportunity] = []
        venues = [venue for venue in self.config.enabled_venues if self.config.venues().get(venue, None) and self.config.venues()[venue].enabled]
        specs = self.config.market_specs()
        for symbol in self.config.symbols:
            spec = specs.get(symbol)
            if not spec:
                continue
            for buy_venue, sell_venue in itertools.permutations(venues, 2):
                buy_book = self.books.get(buy_venue, symbol)
                sell_book = self.books.get(sell_venue, symbol)
                if not buy_book or not sell_book or not buy_book.snapshot_ready or not sell_book.snapshot_ready:
                    continue
                candidate = self._build_candidate(spec, buy_book, sell_book, buy_venue, sell_venue, at_ms)
                if candidate is None:
                    continue
                key = (symbol, buy_venue, sell_venue)
                active = self._active.get(key)
                if active and at_ms - active.last_emitted_ms < self.config.cooldown_ms:
                    continue
                if active is None:
                    active = SpreadState(candidate.detected_at_ms, at_ms)
                    self._active[key] = active
                active.last_seen_ms = at_ms
                active.last_emitted_ms = at_ms
                active.last_spread_pct = candidate.gross_spread_pct
                candidate.spread_age_ms = max(0, at_ms - active.first_seen_ms)
                candidates.append(candidate)
        self._expire(at_ms)
        return candidates

    def _build_candidate(self, spec: MarketSpec, buy_book: OrderBook, sell_book: OrderBook, buy_venue: str, sell_venue: str, at_ms: int) -> Opportunity | None:
        buy_quote = spec.venue_quotes.get(buy_venue, spec.canonical_quote)
        sell_quote = spec.venue_quotes.get(sell_venue, spec.canonical_quote)
        buy_rate = self.config.quote_to_usd.get(buy_quote, 1.0)
        sell_rate = self.config.quote_to_usd.get(sell_quote, 1.0)
        best_ask = buy_book.best_ask()
        best_bid = sell_book.best_bid()
        if not best_ask or not best_bid or best_ask.price <= 0 or best_bid.price <= 0:
            return None
        if max(buy_book.age_ms(at_ms), sell_book.age_ms(at_ms)) > self.config.max_market_data_age_ms:
            return None
        requested_notional = min(self.config.requested_trade_notional_usd, self.config.max_capital_per_opportunity_usd)
        desired_qty = requested_notional / (best_ask.price * buy_rate)
        depth_qty = min(sum(level.quantity for level in buy_book.ask_levels(25)), sum(level.quantity for level in sell_book.bid_levels(25)))
        quantity = quantize_down(min(desired_qty, depth_qty), spec.base_increment)
        if quantity < spec.min_base_qty or quantity <= 0:
            return None
        buy_walk = walk_levels(buy_book.ask_levels(25), quantity)
        sell_walk = walk_levels(sell_book.bid_levels(25), quantity)
        if not buy_walk.complete or not sell_walk.complete:
            return None
        if quantity < desired_qty * self.config.min_fill_ratio:
            return None
        buy_vwap_usd = buy_walk.vwap * buy_rate
        sell_vwap_usd = sell_walk.vwap * sell_rate
        buy_best_usd = best_ask.price * buy_rate
        sell_best_usd = best_bid.price * sell_rate
        buy_cost_usd = buy_walk.quote_amount * buy_rate
        sell_proceeds_usd = sell_walk.quote_amount * sell_rate
        gross_spread_pct = (sell_vwap_usd / buy_vwap_usd - 1.0) * 100.0 if buy_vwap_usd else 0.0
        buy_fee = buy_cost_usd * self.config.venues()[buy_venue].taker_fee_pct
        sell_fee = sell_proceeds_usd * self.config.venues()[sell_venue].taker_fee_pct
        rebalance = buy_cost_usd * self.config.rebalancing_cost_pct
        net_profit = sell_proceeds_usd - buy_cost_usd - buy_fee - sell_fee - rebalance
        expected_net_pct = net_profit / buy_cost_usd * 100.0 if buy_cost_usd else 0.0
        if net_profit <= self.config.candidate_min_net_profit_usd:
            return None
        slippage_buy_pct = (buy_vwap_usd / buy_best_usd - 1.0) * 100.0 if buy_best_usd else 0.0
        slippage_sell_pct = (sell_best_usd / sell_vwap_usd - 1.0) * 100.0 if sell_vwap_usd else 0.0
        estimated_slippage = max(0.0, (slippage_buy_pct + slippage_sell_pct) / 2.0)
        spread = self._spread_stats(at_ms, spec.canonical_symbol, buy_venue, sell_venue, gross_spread_pct)
        volatility_1s = self._volatility(at_ms, buy_venue, spec.canonical_symbol, 1000)
        volatility_5s = self._volatility(at_ms, buy_venue, spec.canonical_symbol, 5000)
        return Opportunity(
            run_id=self.run_id,
            detected_at_ms=at_ms,
            symbol=spec.canonical_symbol,
            base=spec.base,
            buy_venue=buy_venue,
            sell_venue=sell_venue,
            buy_quote=buy_quote,
            sell_quote=sell_quote,
            requested_notional_usd=requested_notional,
            quantity=quantity,
            buy_best_price=best_ask.price,
            sell_best_price=best_bid.price,
            buy_vwap_native=buy_walk.vwap,
            sell_vwap_native=sell_walk.vwap,
            buy_vwap_usd=buy_vwap_usd,
            sell_vwap_usd=sell_vwap_usd,
            buy_depth_usd=sum(level.price * level.quantity * buy_rate for level in buy_book.ask_levels(25)),
            sell_depth_usd=sum(level.price * level.quantity * sell_rate for level in sell_book.bid_levels(25)),
            gross_spread_pct=gross_spread_pct,
            expected_net_spread_pct=expected_net_pct,
            expected_profit_usd=net_profit,
            buy_fee_usd=buy_fee,
            sell_fee_usd=sell_fee,
            estimated_slippage_pct=estimated_slippage,
            rebalance_cost_estimate_usd=rebalance,
            market_data_age_ms=max(buy_book.age_ms(at_ms), sell_book.age_ms(at_ms)),
            buy_latency_ms=self.config.venues()[buy_venue].simulated_latency_ms,
            sell_latency_ms=self.config.venues()[sell_venue].simulated_latency_ms,
            spread_age_ms=spread[0],
            spread_change_100ms_pct=spread[1],
            spread_change_500ms_pct=spread[2],
            volatility_1s_pct=volatility_1s,
            volatility_5s_pct=volatility_5s,
            book_imbalance_buy=buy_book.imbalance(),
            book_imbalance_sell=sell_book.imbalance(),
            inventory_reference={
                "buy": self.reference_inventory.state_for(Opportunity(symbol=spec.canonical_symbol, base=spec.base, buy_venue=buy_venue, sell_venue=sell_venue, buy_quote=buy_quote, sell_quote=sell_quote, quantity=quantity, buy_vwap_native=buy_walk.vwap)),
            },
            constraints={
                "base_increment": spec.base_increment,
                "price_increment": spec.price_increment,
                "min_base_qty": spec.min_base_qty,
                "quote_conversion_buy_to_usd": buy_rate,
                "quote_conversion_sell_to_usd": sell_rate,
                "stablecoin_equivalence_assumed": self.config.stablecoin_equivalence_assumed,
            },
            source_book_sequences={buy_venue: buy_book.sequence, sell_venue: sell_book.sequence},
        )

    def _spread_stats(self, at_ms: int, symbol: str, buy: str, sell: str, current: float) -> tuple[int, float, float]:
        key = (symbol, buy, sell)
        history = self._spread_history.setdefault(key, collections.deque())
        history.append((at_ms, current))
        while history and at_ms - history[0][0] > 5000:
            history.popleft()
        state = self._active.get(key)
        age = max(0, at_ms - state.first_seen_ms) if state else 0
        change_100 = current - _prior(history, at_ms - 100)
        change_500 = current - _prior(history, at_ms - 500)
        return age, change_100, change_500

    def _volatility(self, at_ms: int, venue: str, symbol: str, window_ms: int) -> float:
        book = self.books.get(venue, symbol)
        if not book or not book.mid_price():
            return 0.0
        key = (venue, symbol)
        history = self._mid_history.setdefault(key, collections.deque())
        history.append((at_ms, book.mid_price() or 0.0))
        while history and at_ms - history[0][0] > 5000:
            history.popleft()
        values = [value for timestamp, value in history if at_ms - timestamp <= window_ms and value > 0]
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        return math.sqrt(sum(((value / mean - 1.0) ** 2) for value in values) / len(values)) * 100.0 if mean else 0.0

    def _expire(self, at_ms: int) -> None:
        expired = [key for key, state in self._active.items() if at_ms - state.last_seen_ms > max(1000, self.config.cooldown_ms * 3)]
        for key in expired:
            self._active.pop(key, None)


def _prior(history: collections.deque[tuple[int, float]], target_ms: int) -> float:
    prior = history[0][1] if history else 0.0
    for timestamp, value in history:
        if timestamp <= target_ms:
            prior = value
        else:
            break
    return prior


