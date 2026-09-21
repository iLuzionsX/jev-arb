from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from . import SAFETY_MARKER
from .candidates import CandidateDetector
from .config import AppConfig
from .db import Database
from .domain import BookEvent, Decision, Opportunity, now_ms
from .exchanges import MarketDataManager
from .inventory import InventoryBook
from .jev import FailClosedJevProvider, ReplayJevProvider, TypeSafeJevProvider
from .orderbook import BookStore
from .simulator import ExecutionSimulator
from .strategies import DeterministicStrategy, JevAssistedStrategy


class ExperimentRuntime:
    def __init__(self, config: AppConfig, db: Database, mode: str = "live", provider: Any | None = None):
        self.config = config
        self.db = db
        self.mode = mode
        self.replay_mode = mode == "replay"
        self.books = BookStore()
        self.run_id = self.db.start_run(config, mode, notes="PHASE 1 PAPER TRADING ONLY")
        self.reference_inventory = InventoryBook.seeded(config)
        self.baseline_inventory = InventoryBook.seeded(config)
        self.jev_inventory = InventoryBook.seeded(config)
        self.detector = CandidateDetector(config, self.books, self.run_id, self.reference_inventory)
        if provider is None:
            if os.getenv("TYPESAFE_API_KEY"):
                provider = TypeSafeJevProvider(
                    os.getenv("TYPESAFE_API_KEY"), config.jev_endpoint, config.jev_model,
                    config.jev_timeout_ms, config.jev_cache_path,
                )
            else:
                provider = FailClosedJevProvider()
        self.baseline_strategy = DeterministicStrategy(config)
        self.jev_strategy = JevAssistedStrategy(config, provider)
        self.market_manager = MarketDataManager(config, self.books, self.on_market_event, self.on_health)
        self.simulator = ExecutionSimulator(config, self._book_provider)
        self._portfolio_locks = {"baseline": asyncio.Lock(), "jev": asyncio.Lock()}
        self._pending: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._last_record_ms = 0
        self._last_inventory_record_ms = 0

    async def on_market_event(self, event: BookEvent, quote: str) -> None:
        self.books.apply_event(event, quote)

    async def on_health(self, venue: str, health: dict) -> None:
        self.db.record_health(self.run_id, venue, health)

    async def run_live(self, duration_seconds: float) -> None:
        await self.market_manager.start()
        detector_task = asyncio.create_task(self._candidate_loop(), name="candidate-detector")
        recorder_task = asyncio.create_task(self._recorder_loop(), name="market-recorder")
        try:
            await asyncio.sleep(max(0.0, duration_seconds))
        finally:
            self._stop.set()
            await self.market_manager.stop()
            detector_task.cancel()
            recorder_task.cancel()
            await asyncio.gather(detector_task, recorder_task, return_exceptions=True)
            await self._drain_pending()
            self.db.finish_run(self.run_id)

    async def run_replay_events(self, events: list[dict]) -> None:
        # First pass loads every future snapshot into the replay index so a
        # decision at t can inspect the book at t + latency without lookahead
        # leaking into candidate generation.
        grouped: dict[int, list[dict]] = {}
        for item in events:
            ts = int(item["ts_ms"])
            grouped.setdefault(ts, []).append(item)
        for ts in sorted(grouped):
            for item in grouped[ts]:
                event = event_from_fixture(item)
                self.books.apply_event(event, item.get("quote", "USD"))
            for snapshot in self.books.record_snapshots(ts):
                self.db.record_snapshot(self.run_id, snapshot, ts)
        for ts in sorted(grouped):
            for item in grouped[ts]:
                snapshot = dict(item["book"])
                snapshot.setdefault("updated_at_ms", ts)
                snapshot.setdefault("exchange_ts_ms", item.get("exchange_ts_ms", ts))
                snapshot.setdefault("sequence", item.get("sequence"))
                self.books.replace_current(item["venue"], item["symbol"], snapshot)
            for candidate in self.detector.detect(ts):
                self._schedule(self.handle_candidate(candidate, replay=True))
            await asyncio.sleep(0)
        await self._drain_pending()
        if grouped:
            self.db.set_run_window(self.run_id, min(grouped), max(grouped))
            self.db.finish_run(self.run_id, max(grouped))
        else:
            self.db.finish_run(self.run_id)

    async def _candidate_loop(self) -> None:
        while not self._stop.is_set():
            ts = now_ms()
            for candidate in self.detector.detect(ts):
                self._schedule(self.handle_candidate(candidate, replay=False))
            await asyncio.sleep(self.config.candidate_poll_ms / 1000.0)

    async def _recorder_loop(self) -> None:
        while not self._stop.is_set():
            ts = now_ms()
            if self.config.record_market_snapshots and ts - self._last_record_ms >= self.config.book_record_ms:
                for snapshot in self.books.record_snapshots(ts):
                    self.db.record_snapshot(self.run_id, snapshot, ts)
                self._last_record_ms = ts
            if ts - self._last_inventory_record_ms >= 1000:
                self.db.record_inventory(self.run_id, "baseline", self.baseline_inventory.snapshot(), ts)
                self.db.record_inventory(self.run_id, "jev", self.jev_inventory.snapshot(), ts)
                self._last_inventory_record_ms = ts
            await asyncio.sleep(max(0.02, self.config.book_record_ms / 1000.0 / 2))

    async def handle_candidate(self, candidate: Opportunity, replay: bool) -> None:
        self.db.record_candidate(candidate)
        for strategy_name in ("baseline", "jev"):
            strategy_task = self._evaluate_and_simulate(candidate, strategy_name, replay)
            self._schedule(strategy_task)

    async def _evaluate_and_simulate(self, candidate: Opportunity, strategy_name: str, replay: bool) -> None:
        portfolio = self.baseline_inventory if strategy_name == "baseline" else self.jev_inventory
        strategy = self.baseline_strategy if strategy_name == "baseline" else self.jev_strategy
        decision = await strategy.evaluate(candidate, portfolio)
        if replay:
            decision.decision_at_ms = candidate.detected_at_ms + int(max(0.0, decision.latency_ms))
        self.db.record_decision(decision)
        exchange_latency = max(candidate.buy_latency_ms, candidate.sell_latency_ms)
        arrival_at = decision.decision_at_ms + exchange_latency
        if not replay:
            await asyncio.sleep(max(0.0, (arrival_at - now_ms()) / 1000.0))
            arrival_at = now_ms()
        if decision.accepted:
            async with self._portfolio_locks[strategy_name]:
                trade = self.simulator.simulate(candidate, strategy_name, True, decision.decision_at_ms, arrival_at, portfolio, mutate=True)
        else:
            counterfactual = portfolio.clone()
            trade = self.simulator.simulate(candidate, strategy_name, False, decision.decision_at_ms, arrival_at, counterfactual, mutate=False)
            if trade.status == "executed":
                trade.status = "counterfactual_rejected"
            elif trade.status == "partial_fill":
                trade.status = "counterfactual_partial_rejected"
            elif trade.status == "one_leg_fill":
                trade.status = "counterfactual_one_leg_rejected"
        self.db.record_trade(trade)
        if decision.accepted:
            self.db.record_inventory(self.run_id, strategy_name, portfolio.snapshot(), arrival_at)

    def _book_provider(self, venue: str, symbol: str, at_ms: int):
        if self.replay_mode:
            return self.books.snapshot_at(venue, symbol, at_ms)
        return self.books.get(venue, symbol)

    def _schedule(self, awaitable) -> None:
        task = asyncio.create_task(awaitable)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _drain_pending(self) -> None:
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)


def event_from_fixture(item: dict) -> BookEvent:
    return BookEvent(
        venue=item["venue"],
        symbol=item["symbol"],
        kind="snapshot",
        bids=[(float(level[0]), float(level[1])) for level in item["book"].get("bids", [])],
        asks=[(float(level[0]), float(level[1])) for level in item["book"].get("asks", [])],
        received_at_ms=int(item["ts_ms"]),
        exchange_ts_ms=int(item.get("exchange_ts_ms", item["ts_ms"])),
        sequence=item.get("sequence"),
        event_id=item.get("event_id") or f"fixture:{item['ts_ms']}:{item['venue']}:{item['symbol']}",
        raw=item,
    )

