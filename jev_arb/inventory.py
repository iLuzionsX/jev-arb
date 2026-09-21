from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .config import AppConfig, MarketSpec
from .domain import Fill, Opportunity


@dataclass
class InventoryBook:
    balances: dict[str, dict[str, float]]

    @classmethod
    def seeded(cls, config: AppConfig) -> "InventoryBook":
        balances: dict[str, dict[str, float]] = {}
        specs = config.market_specs()
        active_venues = [venue for venue in config.enabled_venues if venue in config.venue_allocations_usd]
        allocations = sum(config.venue_allocations_usd.get(venue, 0.0) for venue in active_venues)
        scale = ((config.paper_capital_usd - config.reserve_usd) / allocations) if allocations else 1.0
        for venue in active_venues:
            allocation = config.venue_allocations_usd.get(venue, 0.0) * scale
            quote_value = allocation * (1.0 - config.base_inventory_fraction)
            base_value = allocation * config.base_inventory_fraction
            venue_balances: dict[str, float] = {}
            native_quote = "USD"
            for symbol in config.symbols:
                spec = specs.get(symbol)
                if spec:
                    native_quote = spec.venue_quotes.get(venue, native_quote)
                    break
            venue_balances[native_quote] = quote_value
            for symbol, weight in config.base_inventory_weights.items():
                spec = specs.get(symbol)
                if not spec:
                    continue
                price = max(1e-12, config.reference_prices_usd.get(symbol, 1.0))
                venue_balances[spec.base] = base_value * weight / price
            balances[venue] = venue_balances
        return cls(balances)

    def clone(self) -> "InventoryBook":
        return InventoryBook(copy.deepcopy(self.balances))

    def balance(self, venue: str, currency: str) -> float:
        return float(self.balances.get(venue, {}).get(currency, 0.0))

    def can_buy(self, candidate: Opportunity, fee_pct: float) -> bool:
        rate = 1.0
        if candidate.buy_quote in {"USDC", "USDT", "USD"}:
            # The candidate's USD-normalized amount is a conservative check;
            # the precise conversion assumption is recorded in the run config.
            rate = 1.0
        native_cost = candidate.buy_vwap_native * candidate.quantity
        return self.balance(candidate.buy_venue, candidate.buy_quote) >= native_cost * (1.0 + fee_pct) / rate

    def can_sell(self, candidate: Opportunity) -> bool:
        return self.balance(candidate.sell_venue, candidate.base) >= candidate.quantity

    def can_execute(self, candidate: Opportunity, fee_pct_buy: float) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if not self.can_buy(candidate, fee_pct_buy):
            reasons.append("insufficient_buy_quote_inventory")
        if not self.can_sell(candidate):
            reasons.append("insufficient_sell_base_inventory")
        return not reasons, reasons

    def apply_buy(self, venue: str, base: str, quote: str, quantity: float, quote_amount: float, fee_usd: float, quote_to_usd: float = 1.0) -> None:
        venue_balances = self.balances.setdefault(venue, {})
        venue_balances[quote] = venue_balances.get(quote, 0.0) - quote_amount - fee_usd / max(quote_to_usd, 1e-12)
        venue_balances[base] = venue_balances.get(base, 0.0) + quantity

    def apply_sell(self, venue: str, base: str, quote: str, quantity: float, quote_amount: float, fee_usd: float, quote_to_usd: float = 1.0) -> None:
        venue_balances = self.balances.setdefault(venue, {})
        venue_balances[base] = venue_balances.get(base, 0.0) - quantity
        venue_balances[quote] = venue_balances.get(quote, 0.0) + quote_amount - fee_usd / max(quote_to_usd, 1e-12)

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {venue: {currency: round(value, 12) for currency, value in balances.items()}
                for venue, balances in self.balances.items()}

    def state_for(self, candidate: Opportunity) -> dict[str, Any]:
        return {
            "buy_venue": candidate.buy_venue,
            "sell_venue": candidate.sell_venue,
            "buy_quote": candidate.buy_quote,
            "sell_quote": candidate.sell_quote,
            "base": candidate.base,
            "buy_quote_balance": self.balance(candidate.buy_venue, candidate.buy_quote),
            "sell_base_balance": self.balance(candidate.sell_venue, candidate.base),
            "requested_quantity": candidate.quantity,
            "can_buy": self.can_buy(candidate, 0.0),
            "can_sell": self.can_sell(candidate),
        }


