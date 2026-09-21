from __future__ import annotations

import bisect
import threading
from dataclasses import dataclass, field
from typing import Iterable

from .domain import BookEvent, now_ms


@dataclass(frozen=True)
class BookLevel:
    price: float
    quantity: float


@dataclass
class OrderBook:
    venue: str
    symbol: str
    quote: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    updated_at_ms: int = 0
    exchange_ts_ms: int | None = None
    sequence: int | None = None
    snapshot_ready: bool = False
    gap_detected: bool = False
    last_event_id: str | None = None

    def apply(self, event: BookEvent) -> bool:
        if event.kind == "snapshot":
            self.bids = {float(p): float(q) for p, q in event.bids if float(q) > 0}
            self.asks = {float(p): float(q) for p, q in event.asks if float(q) > 0}
            self.snapshot_ready = True
            self.gap_detected = False
        else:
            for price, quantity in event.bids:
                _set_level(self.bids, float(price), float(quantity))
            for price, quantity in event.asks:
                _set_level(self.asks, float(price), float(quantity))
        self.updated_at_ms = event.received_at_ms
        self.exchange_ts_ms = event.exchange_ts_ms
        self.sequence = event.sequence if event.sequence is not None else self.sequence
        self.last_event_id = event.event_id
        return True

    def best_bid(self) -> BookLevel | None:
        if not self.bids:
            return None
        price = max(self.bids)
        return BookLevel(price, self.bids[price])

    def best_ask(self) -> BookLevel | None:
        if not self.asks:
            return None
        price = min(self.asks)
        return BookLevel(price, self.asks[price])

    def bid_levels(self, depth: int = 25) -> list[BookLevel]:
        return [BookLevel(p, self.bids[p]) for p in sorted(self.bids, reverse=True)[:depth]]

    def ask_levels(self, depth: int = 25) -> list[BookLevel]:
        return [BookLevel(p, self.asks[p]) for p in sorted(self.asks)[:depth]]

    def age_ms(self, at_ms: int | None = None) -> int:
        if self.updated_at_ms <= 0:
            return 2**31 - 1
        return max(0, (at_ms or now_ms()) - self.updated_at_ms)

    def imbalance(self, depth: int = 5) -> float:
        bid_qty = sum(level.quantity for level in self.bid_levels(depth))
        ask_qty = sum(level.quantity for level in self.ask_levels(depth))
        total = bid_qty + ask_qty
        return (bid_qty - ask_qty) / total if total else 0.0

    def mid_price(self) -> float | None:
        bid = self.best_bid()
        ask = self.best_ask()
        if not bid or not ask:
            return None
        return (bid.price + ask.price) / 2.0

    def to_dict(self, depth: int = 25) -> dict:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "quote": self.quote,
            "updated_at_ms": self.updated_at_ms,
            "exchange_ts_ms": self.exchange_ts_ms,
            "sequence": self.sequence,
            "snapshot_ready": self.snapshot_ready,
            "gap_detected": self.gap_detected,
            "bids": [[level.price, level.quantity] for level in self.bid_levels(depth)],
            "asks": [[level.price, level.quantity] for level in self.ask_levels(depth)],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OrderBook":
        book = cls(venue=data["venue"], symbol=data["symbol"], quote=data.get("quote", "USD"))
        book.bids = {float(p): float(q) for p, q in data.get("bids", []) if float(q) > 0}
        book.asks = {float(p): float(q) for p, q in data.get("asks", []) if float(q) > 0}
        book.updated_at_ms = int(data.get("updated_at_ms", 0))
        book.exchange_ts_ms = data.get("exchange_ts_ms")
        book.sequence = data.get("sequence")
        book.snapshot_ready = bool(data.get("snapshot_ready", True))
        book.gap_detected = bool(data.get("gap_detected", False))
        return book


def _set_level(side: dict[float, float], price: float, quantity: float) -> None:
    if quantity <= 0:
        side.pop(price, None)
    else:
        side[price] = quantity


@dataclass(frozen=True)
class WalkResult:
    requested_qty: float
    filled_qty: float
    quote_amount: float
    vwap: float
    complete: bool


def walk_levels(levels: Iterable[BookLevel], requested_qty: float) -> WalkResult:
    remaining = max(0.0, requested_qty)
    quote_amount = 0.0
    filled = 0.0
    for level in levels:
        if remaining <= 1e-15:
            break
        take = min(remaining, max(0.0, level.quantity))
        filled += take
        quote_amount += take * level.price
        remaining -= take
    return WalkResult(
        requested_qty=requested_qty,
        filled_qty=filled,
        quote_amount=quote_amount,
        vwap=quote_amount / filled if filled > 0 else 0.0,
        complete=remaining <= max(1e-12, requested_qty * 1e-9),
    )


def quantize_down(value: float, increment: float) -> float:
    if increment <= 0:
        return value
    return max(0.0, int(value / increment + 1e-12) * increment)


class BookStore:
    """Thread-safe current books plus bounded in-memory replay history."""

    def __init__(self, history_limit: int = 20_000):
        self._books: dict[tuple[str, str], OrderBook] = {}
        self._history: dict[tuple[str, str], list[tuple[int, dict]]] = {}
        self._last_event_ids: set[str] = set()
        self._health: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.history_limit = history_limit

    def apply_event(self, event: BookEvent, quote: str) -> OrderBook:
        key = (event.venue, event.symbol)
        with self._lock:
            if event.event_id and event.event_id in self._last_event_ids:
                return self._books[key]
            book = self._books.get(key)
            if book is None:
                book = OrderBook(event.venue, event.symbol, quote)
                self._books[key] = book
            if event.kind == "delta" and event.sequence is not None and book.sequence is not None:
                if event.sequence < book.sequence:
                    return book
                if event.sequence > book.sequence + 1:
                    # The connector will reconnect/reset; do not silently call
                    # a gap-composed book valid.
                    book.snapshot_ready = False
                    book.gap_detected = True
            book.apply(event)
            if event.event_id:
                self._last_event_ids.add(event.event_id)
                if len(self._last_event_ids) > self.history_limit * 2:
                    self._last_event_ids = set(list(self._last_event_ids)[-self.history_limit:])
            return book

    def record_snapshots(self, ts_ms: int | None = None) -> list[dict]:
        with self._lock:
            timestamp = ts_ms or now_ms()
            snapshots: list[dict] = []
            for key, book in self._books.items():
                if not book.snapshot_ready:
                    continue
                payload = book.to_dict()
                self._history.setdefault(key, []).append((timestamp, payload))
                if len(self._history[key]) > self.history_limit:
                    self._history[key] = self._history[key][-self.history_limit:]
                snapshots.append(payload)
            return snapshots

    def get(self, venue: str, symbol: str) -> OrderBook | None:
        with self._lock:
            book = self._books.get((venue, symbol))
            if book is None:
                return None
            return OrderBook.from_dict(book.to_dict())

    def replace_current(self, venue: str, symbol: str, snapshot: dict) -> None:
        with self._lock:
            payload = dict(snapshot)
            payload.setdefault("venue", venue)
            payload.setdefault("symbol", symbol)
            payload.setdefault("quote", "USD")
            payload.setdefault("snapshot_ready", True)
            self._books[(venue, symbol)] = OrderBook.from_dict(payload)

    def snapshot_at(self, venue: str, symbol: str, ts_ms: int) -> OrderBook | None:
        with self._lock:
            history = self._history.get((venue, symbol), [])
            if not history:
                return self.get(venue, symbol)
            times = [item[0] for item in history]
            index = bisect.bisect_left(times, ts_ms)
            if index >= len(history):
                return None
            return OrderBook.from_dict(history[index][1])

    def all_current(self) -> list[OrderBook]:
        with self._lock:
            return [OrderBook.from_dict(book.to_dict()) for book in self._books.values()]

    def set_health(self, venue: str, health: dict) -> None:
        with self._lock:
            self._health[venue] = dict(health)

    def health(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._health.items()}

