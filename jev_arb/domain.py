from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


def now_ms() -> int:
    return int(time.time() * 1000)


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class BookEvent:
    venue: str
    symbol: str
    kind: Literal["snapshot", "delta"]
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    received_at_ms: int
    exchange_ts_ms: int | None = None
    sequence: int | None = None
    event_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Opportunity:
    candidate_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = ""
    detected_at_ms: int = field(default_factory=now_ms)
    symbol: str = ""
    base: str = ""
    buy_venue: str = ""
    sell_venue: str = ""
    buy_quote: str = ""
    sell_quote: str = ""
    requested_notional_usd: float = 0.0
    quantity: float = 0.0
    buy_best_price: float = 0.0
    sell_best_price: float = 0.0
    buy_vwap_native: float = 0.0
    sell_vwap_native: float = 0.0
    buy_vwap_usd: float = 0.0
    sell_vwap_usd: float = 0.0
    buy_depth_usd: float = 0.0
    sell_depth_usd: float = 0.0
    gross_spread_pct: float = 0.0
    expected_net_spread_pct: float = 0.0
    expected_profit_usd: float = 0.0
    buy_fee_usd: float = 0.0
    sell_fee_usd: float = 0.0
    estimated_slippage_pct: float = 0.0
    rebalance_cost_estimate_usd: float = 0.0
    market_data_age_ms: int = 0
    buy_latency_ms: int = 0
    sell_latency_ms: int = 0
    spread_age_ms: int = 0
    spread_change_100ms_pct: float = 0.0
    spread_change_500ms_pct: float = 0.0
    volatility_1s_pct: float = 0.0
    volatility_5s_pct: float = 0.0
    book_imbalance_buy: float = 0.0
    book_imbalance_sell: float = 0.0
    inventory_reference: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    source_book_sequences: dict[str, int | None] = field(default_factory=dict)
    lifecycle: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def jev_state(self) -> dict[str, Any]:
        # This is deliberately compact and contains only research state. Jev
        # is not allowed to derive execution math or mutate this object.
        return {
            "symbol": self.symbol,
            "buy_exchange": self.buy_venue,
            "sell_exchange": self.sell_venue,
            "trade_notional_usd": round(self.requested_notional_usd, 8),
            "quantity_base": round(self.quantity, 12),
            "gross_spread_pct": round(self.gross_spread_pct, 8),
            "expected_net_spread_pct": round(self.expected_net_spread_pct, 8),
            "expected_profit_usd": round(self.expected_profit_usd, 8),
            "buy_depth_usd": round(self.buy_depth_usd, 8),
            "sell_depth_usd": round(self.sell_depth_usd, 8),
            "estimated_slippage_pct": round(self.estimated_slippage_pct, 8),
            "market_data_age_ms": self.market_data_age_ms,
            "buy_latency_ms": self.buy_latency_ms,
            "sell_latency_ms": self.sell_latency_ms,
            "spread_age_ms": self.spread_age_ms,
            "spread_change_100ms_pct": round(self.spread_change_100ms_pct, 8),
            "spread_change_500ms_pct": round(self.spread_change_500ms_pct, 8),
            "volatility_1s_pct": round(self.volatility_1s_pct, 8),
            "volatility_5s_pct": round(self.volatility_5s_pct, 8),
            "book_imbalance_buy": round(self.book_imbalance_buy, 8),
            "book_imbalance_sell": round(self.book_imbalance_sell, 8),
            "inventory_state": self.inventory_reference,
            "constraints": self.constraints,
        }


@dataclass
class Decision:
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    candidate_id: str = ""
    strategy: str = ""
    decision: str = "SKIP"
    reason: str = ""
    accepted: bool = False
    decision_at_ms: int = field(default_factory=now_ms)
    latency_ms: float = 0.0
    confidence: float | None = None
    input_state: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Fill:
    fill_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    venue: str = ""
    side: str = ""
    base_qty: float = 0.0
    quote_amount: float = 0.0
    vwap: float = 0.0
    fee_usd: float = 0.0
    quote: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradeResult:
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    candidate_id: str = ""
    strategy: str = ""
    accepted: bool = False
    status: str = "missed_opportunity"
    detection_at_ms: int = 0
    decision_at_ms: int = 0
    arrival_at_ms: int = 0
    expected_pnl_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    mark_to_market_pnl_usd: float = 0.0
    net_cashflow_usd: float = 0.0
    filled_buy_qty: float = 0.0
    filled_sell_qty: float = 0.0
    expected_vs_realized_delta_usd: float = 0.0
    buy_fill: Fill | None = None
    sell_fill: Fill | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


