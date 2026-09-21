from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import urllib.parse
import urllib.request
from typing import Any, Awaitable, Callable

import websockets

from .config import AppConfig, MarketSpec
from .domain import BookEvent
from .orderbook import BookStore


EventCallback = Callable[[BookEvent, str], Awaitable[None] | None]
HealthCallback = Callable[[str, dict], Awaitable[None] | None]


class BaseConnector:
    venue = ""
    url = ""

    def __init__(self, config: AppConfig, books: BookStore, on_event: EventCallback | None = None, on_health: HealthCallback | None = None):
        self.config = config
        self.books = books
        self.on_event = on_event
        self.on_health = on_health
        self.stop_event = asyncio.Event()
        self.reconnects = 0
        self.last_message_ms: int | None = None
        self.last_error: str | None = None

    async def run(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                await self._set_health(True, None)
                await self.run_once()
                backoff = 1.0
            except asyncio.CancelledError:
                await self._set_health(False, "stopped")
                raise
            except Exception as exc:
                self.reconnects += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                await self._set_health(False, self.last_error)
                await asyncio.sleep(min(backoff, self.config.exchange_reconnect_max_seconds))
                backoff = min(backoff * 2, self.config.exchange_reconnect_max_seconds)
        await self._set_health(False, "stopped")

    async def stop(self) -> None:
        self.stop_event.set()

    async def run_once(self) -> None:
        raise NotImplementedError

    async def emit(self, event: BookEvent) -> None:
        self.last_message_ms = event.received_at_ms
        if self.on_event:
            result = self.on_event(event, self._quote_for(event.symbol))
            if asyncio.iscoroutine(result):
                await result
        book = self.books.get(self.venue, event.symbol)
        if book and book.gap_detected:
            raise RuntimeError(f"sequence gap detected for {self.venue}/{event.symbol}; reconnecting for a fresh snapshot")
        await self._set_health(True, None)

    async def _set_health(self, connected: bool, error: str | None) -> None:
        health = {
            "connected": connected,
            "last_message_ms": self.last_message_ms,
            "reconnects": self.reconnects,
            "last_error": error or self.last_error,
            "url": self.url,
            "public_read_only": True,
        }
        self.books.set_health(self.venue, health)
        if self.on_health:
            result = self.on_health(self.venue, health)
            if asyncio.iscoroutine(result):
                await result

    def _quote_for(self, symbol: str) -> str:
        spec = self.config.market_specs()[symbol]
        return spec.venue_quotes[self.venue]


class CoinbaseConnector(BaseConnector):
    venue = "coinbase"
    url = "wss://advanced-trade-ws.coinbase.com"

    async def run_once(self) -> None:
        products = [self.config.market_specs()[symbol].venue_symbols[self.venue] for symbol in self.config.symbols if symbol in self.config.market_specs()]
        async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "subscribe", "product_ids": products, "channel": "level2"}))
            async for raw in ws:
                if self.stop_event.is_set():
                    break
                data = json.loads(raw)
                if data.get("channel") != "l2_data":
                    continue
                for event in data.get("events", []):
                    product = event.get("product_id")
                    symbol = self._canonical(product)
                    if not symbol:
                        continue
                    bids: list[tuple[float, float]] = []
                    asks: list[tuple[float, float]] = []
                    for update in event.get("updates", []):
                        pair = (float(update.get("price_level", 0)), float(update.get("new_quantity", 0)))
                        if update.get("side") == "bid":
                            bids.append(pair)
                        elif update.get("side") == "offer" or update.get("side") == "ask":
                            asks.append(pair)
                    exchange_ts = parse_timestamp(data.get("timestamp") or event.get("event_time"))
                    await self.emit(BookEvent(
                        venue=self.venue,
                        symbol=symbol,
                        kind="snapshot" if event.get("type") == "snapshot" else "delta",
                        bids=bids,
                        asks=asks,
                        received_at_ms=int(time.time() * 1000),
                        exchange_ts_ms=exchange_ts,
                        sequence=_int_or_none(data.get("sequence_num")),
                        event_id=f"coinbase:{data.get('sequence_num')}:{product}:{event.get('type')}:{data.get('timestamp')}",
                        raw=data,
                    ))

    def _canonical(self, product: str | None) -> str | None:
        for symbol, spec in self.config.market_specs().items():
            if spec.venue_symbols.get(self.venue) == product:
                return symbol
        return None


class KrakenConnector(BaseConnector):
    venue = "kraken"
    url = "wss://ws.kraken.com/v2"

    async def run_once(self) -> None:
        symbols = [self.config.market_specs()[symbol].venue_symbols[self.venue] for symbol in self.config.symbols if symbol in self.config.market_specs()]
        async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "book", "symbol": symbols, "depth": 25, "snapshot": True}, "req_id": 1}))
            async for raw in ws:
                if self.stop_event.is_set():
                    break
                data = json.loads(raw)
                if data.get("channel") != "book":
                    continue
                kind = "snapshot" if data.get("type") == "snapshot" else "delta"
                for item in data.get("data", []):
                    symbol = self._canonical(item.get("symbol"))
                    if not symbol:
                        continue
                    bids = [(float(level.get("price", 0)), float(level.get("qty", 0))) for level in item.get("bids", [])]
                    asks = [(float(level.get("price", 0)), float(level.get("qty", 0))) for level in item.get("asks", [])]
                    await self.emit(BookEvent(
                        venue=self.venue,
                        symbol=symbol,
                        kind=kind,
                        bids=bids,
                        asks=asks,
                        received_at_ms=int(time.time() * 1000),
                        exchange_ts_ms=parse_timestamp(item.get("timestamp")),
                        sequence=None,
                        event_id=f"kraken:{item.get('symbol')}:{item.get('timestamp')}:{kind}",
                        raw=data,
                    ))

    def _canonical(self, symbol: str | None) -> str | None:
        for canonical, spec in self.config.market_specs().items():
            if spec.venue_symbols.get(self.venue) == symbol:
                return canonical
        return None


class BinanceConnector(BaseConnector):
    """Public Spot partial-depth stream; intentionally disabled by default.

    Partial depth is used so the adapter can maintain a coherent bounded book
    without credentials. It is not treated as a complete venue-wide book.
    """

    venue = "binance"
    url = "wss://stream.binance.com:9443/stream"

    async def run_once(self) -> None:
        streams = [f"{self.config.market_specs()[symbol].venue_symbols[self.venue].lower()}@depth20@100ms" for symbol in self.config.symbols if symbol in self.config.market_specs()]
        url = self.url + "?streams=" + "/".join(streams)
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=8 * 1024 * 1024) as ws:
            async for raw in ws:
                if self.stop_event.is_set():
                    break
                data = json.loads(raw)
                payload = data.get("data", data)
                symbol = self._canonical(payload.get("s"))
                if not symbol or payload.get("e") not in {"depthUpdate", None}:
                    continue
                await self.emit(BookEvent(
                    venue=self.venue,
                    symbol=symbol,
                    kind="snapshot",
                    bids=[(float(level[0]), float(level[1])) for level in payload.get("bids", [])],
                    asks=[(float(level[0]), float(level[1])) for level in payload.get("asks", [])],
                    received_at_ms=int(time.time() * 1000),
                    exchange_ts_ms=_int_or_none(payload.get("E")),
                    sequence=_int_or_none(payload.get("u") or payload.get("lastUpdateId")),
                    event_id=f"binance:{payload.get('s')}:{payload.get('u') or payload.get('lastUpdateId')}",
                    raw=data,
                ))

    def _canonical(self, symbol: str | None) -> str | None:
        for canonical, spec in self.config.market_specs().items():
            if spec.venue_symbols.get(self.venue) == symbol:
                return canonical
        return None


class MarketDataManager:
    def __init__(self, config: AppConfig, books: BookStore, on_event: EventCallback | None = None, on_health: HealthCallback | None = None):
        self.connectors: list[BaseConnector] = []
        builders = {"coinbase": CoinbaseConnector, "kraken": KrakenConnector, "binance": BinanceConnector}
        for venue in config.enabled_venues:
            if venue in builders:
                self.connectors.append(builders[venue](config, books, on_event, on_health))
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self.tasks = [asyncio.create_task(connector.run(), name=f"market-{connector.venue}") for connector in self.connectors]

    async def stop(self) -> None:
        for connector in self.connectors:
            await connector.stop()
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


def parse_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 10_000_000_000 else value * 1000)
    try:
        text = str(value).replace("Z", "+00:00")
        return int(dt.datetime.fromisoformat(text).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

