from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MarketSpec:
    canonical_symbol: str
    base: str
    canonical_quote: str
    venue_symbols: dict[str, str]
    venue_quotes: dict[str, str]
    base_increment: float = 1e-8
    price_increment: float = 0.01
    min_base_qty: float = 0.00001


@dataclass(frozen=True)
class VenueConfig:
    name: str
    enabled: bool
    taker_fee_pct: float
    simulated_latency_ms: int
    max_data_age_ms: int = 1500


@dataclass
class AppConfig:
    db_path: str = "data/jev-arb.db"
    enabled_venues: list[str] = field(default_factory=lambda: ["coinbase", "kraken"])
    symbols: list[str] = field(default_factory=lambda: ["BTC-USD", "ETH-USD", "SOL-USD"])
    paper_capital_usd: float = 25_000.0
    reserve_usd: float = 1_000.0
    venue_allocations_usd: dict[str, float] = field(
        default_factory=lambda: {"coinbase": 8_000.0, "kraken": 8_000.0, "binance": 8_000.0}
    )
    base_inventory_fraction: float = 0.50
    base_inventory_weights: dict[str, float] = field(
        default_factory=lambda: {"BTC-USD": 0.60, "ETH-USD": 0.25, "SOL-USD": 0.15}
    )
    reference_prices_usd: dict[str, float] = field(
        default_factory=lambda: {"BTC-USD": 100_000.0, "ETH-USD": 4_000.0, "SOL-USD": 200.0}
    )
    quote_to_usd: dict[str, float] = field(
        default_factory=lambda: {"USD": 1.0, "USDC": 1.0, "USDT": 1.0}
    )
    stablecoin_equivalence_assumed: bool = True
    candidate_poll_ms: int = 250
    book_record_ms: int = 100
    requested_trade_notional_usd: float = 5_000.0
    candidate_min_net_profit_usd: float = 0.0
    min_fill_ratio: float = 0.95
    rebalancing_cost_pct: float = 0.0005
    max_market_data_age_ms: int = 1_500
    max_slippage_pct: float = 0.25
    max_volatility_1s_pct: float = 0.50
    min_spread_persistence_ms: int = 150
    min_expected_net_spread_pct: float = 0.02
    min_expected_profit_usd: float = 1.00
    cooldown_ms: int = 1_000
    max_capital_per_opportunity_usd: float = 5_000.0
    jev_timeout_ms: int = 450
    jev_min_execute_probability: float = 0.55
    jev_min_confidence: float = 0.0
    jev_model: str = "jev-latest"
    jev_endpoint: str = "https://api.typesafe.ai/v1/systemone"
    jev_cache_path: str = "data/jev-cache.json"
    exchange_reconnect_max_seconds: float = 30.0
    record_market_snapshots: bool = True

    def market_specs(self) -> dict[str, MarketSpec]:
        return {
            "BTC-USD": MarketSpec(
                "BTC-USD", "BTC", "USD",
                {"coinbase": "BTC-USD", "kraken": "BTC/USD", "binance": "BTCUSDT"},
                {"coinbase": "USD", "kraken": "USD", "binance": "USDT"},
                base_increment=0.00000001, price_increment=0.01, min_base_qty=0.00001,
            ),
            "ETH-USD": MarketSpec(
                "ETH-USD", "ETH", "USD",
                {"coinbase": "ETH-USD", "kraken": "ETH/USD", "binance": "ETHUSDT"},
                {"coinbase": "USD", "kraken": "USD", "binance": "USDT"},
                base_increment=0.000001, price_increment=0.01, min_base_qty=0.0001,
            ),
            "SOL-USD": MarketSpec(
                "SOL-USD", "SOL", "USD",
                {"coinbase": "SOL-USD", "kraken": "SOL/USD", "binance": "SOLUSDT"},
                {"coinbase": "USD", "kraken": "USD", "binance": "USDT"},
                base_increment=0.000001, price_increment=0.001, min_base_qty=0.01,
            ),
        }

    def venues(self) -> dict[str, VenueConfig]:
        fees = {"coinbase": 0.0060, "kraken": 0.0040, "binance": 0.0010}
        result: dict[str, VenueConfig] = {}
        for name in ("coinbase", "kraken", "binance"):
            result[name] = VenueConfig(
                name=name,
                enabled=name in self.enabled_venues,
                taker_fee_pct=fees[name],
                simulated_latency_ms=self.exchange_latency_for(name),
            )
        return result

    def exchange_latency_for(self, venue: str) -> int:
        # Keep the assumption explicit and configurable via the common latency
        # setting. Venue-specific values can be added without changing the
        # simulator contract.
        _ = venue
        return int(os.getenv("JEV_ARB_EXCHANGE_LATENCY_MS", "75"))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["markets"] = {k: asdict(v) for k, v in self.market_specs().items()}
        data["venues"] = {k: asdict(v) for k, v in self.venues().items()}
        data["safety"] = {
            "paper_only": True,
            "no_exchange_credentials": True,
            "no_order_endpoint": True,
            "no_withdrawal_endpoint": True,
        }
        return data

    @classmethod
    def from_env(cls, db_path: str | None = None) -> "AppConfig":
        cfg = cls()
        cfg.db_path = db_path or os.getenv("JEV_ARB_DB", cfg.db_path)
        cfg.enabled_venues = _csv(os.getenv("JEV_ARB_ENABLED_VENUES"), cfg.enabled_venues)
        cfg.symbols = _csv(os.getenv("JEV_ARB_SYMBOLS"), cfg.symbols)
        cfg.paper_capital_usd = _float("JEV_ARB_PAPER_CAPITAL_USD", cfg.paper_capital_usd)
        cfg.reserve_usd = _float("JEV_ARB_RESERVE_USD", cfg.reserve_usd)
        cfg.candidate_poll_ms = _int("JEV_ARB_CANDIDATE_POLL_MS", cfg.candidate_poll_ms)
        cfg.book_record_ms = _int("JEV_ARB_BOOK_RECORD_MS", cfg.book_record_ms)
        cfg.jev_timeout_ms = _int("JEV_ARB_JEV_TIMEOUT_MS", cfg.jev_timeout_ms)
        cfg.jev_model = os.getenv("TYPESAFE_MODEL", cfg.jev_model)
        cfg.jev_endpoint = os.getenv("TYPESAFE_ENDPOINT", cfg.jev_endpoint)
        cfg.stablecoin_equivalence_assumed = _bool(
            "JEV_ARB_STABLECOIN_EQUIVALENCE_ASSUMED", cfg.stablecoin_equivalence_assumed
        )
        for quote in ("USD", "USDC", "USDT"):
            env_key = f"JEV_ARB_{quote}_TO_USD"
            cfg.quote_to_usd[quote] = _float(env_key, cfg.quote_to_usd[quote])
        return cfg


def _csv(value: str | None, fallback: list[str]) -> list[str]:
    if not value:
        return list(fallback)
    return [item.strip().lower() if item.strip() in {"coinbase", "kraken", "binance"} else item.strip()
            for item in value.split(",") if item.strip()]


def _float(key: str, fallback: float) -> float:
    try:
        return float(os.getenv(key, fallback))
    except (TypeError, ValueError):
        return fallback


def _int(key: str, fallback: int) -> int:
    try:
        return int(os.getenv(key, fallback))
    except (TypeError, ValueError):
        return fallback


def _bool(key: str, fallback: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on"}


def pretty_config(config: AppConfig) -> str:
    return json.dumps(config.to_dict(), indent=2, sort_keys=True)


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


