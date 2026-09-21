from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from jev_arb.candidates import CandidateDetector
from jev_arb.config import AppConfig
from jev_arb.db import Database
from jev_arb.domain import BookEvent, Opportunity
from jev_arb.inventory import InventoryBook
from jev_arb.orderbook import BookStore, BookLevel, walk_levels
from jev_arb.runtime import ExperimentRuntime
from jev_arb.simulator import ExecutionSimulator
from jev_arb.strategies import JevAssistedStrategy, common_safety_gate
from jev_arb.replay import load_jsonl
from jev_arb.jev import ReplayJevProvider, parse_typesafe_response


class CoreTests(unittest.TestCase):
    def test_vwap_walk_and_partial_depth(self):
        result = walk_levels([BookLevel(100.0, 1.0), BookLevel(101.0, 2.0)], 2.0)
        self.assertTrue(result.complete)
        self.assertAlmostEqual(result.quote_amount, 201.0)
        self.assertAlmostEqual(result.vwap, 100.5)
        partial = walk_levels([BookLevel(100.0, 1.0)], 2.0)
        self.assertFalse(partial.complete)
        self.assertEqual(partial.filled_qty, 1.0)

    def test_book_snapshot_delta_and_out_of_order(self):
        books = BookStore()
        books.apply_event(BookEvent("coinbase", "BTC-USD", "snapshot", [(99, 2)], [(101, 2)], 1000, sequence=10, event_id="a"), "USD")
        books.apply_event(BookEvent("coinbase", "BTC-USD", "delta", [(100, 1)], [], 1001, sequence=11, event_id="b"), "USD")
        books.apply_event(BookEvent("coinbase", "BTC-USD", "delta", [(98, 1)], [], 1002, sequence=9, event_id="c"), "USD")
        books.apply_event(BookEvent("coinbase", "BTC-USD", "delta", [(97, 1)], [], 1003, sequence=13, event_id="d"), "USD")
        book = books.get("coinbase", "BTC-USD")
        self.assertIsNotNone(book)
        self.assertEqual(book.best_bid().price, 100)
        self.assertEqual(book.sequence, 13)
        self.assertTrue(book.gap_detected)
        books.apply_event(BookEvent("coinbase", "BTC-USD", "snapshot", [(99, 2)], [(101, 2)], 1004, sequence=14, event_id="e"), "USD")
        self.assertFalse(books.get("coinbase", "BTC-USD").gap_detected)

    def test_candidate_uses_depth_and_after_cost_math(self):
        config = AppConfig()
        config.enabled_venues = ["coinbase", "kraken"]
        config.symbols = ["BTC-USD"]
        config.requested_trade_notional_usd = 1000
        config.max_capital_per_opportunity_usd = 1000
        config.min_expected_net_spread_pct = 0
        config.min_expected_profit_usd = 0
        config.min_spread_persistence_ms = 0
        config.reference_prices_usd["BTC-USD"] = 100_000
        books = BookStore()
        ts = 1_700_000_000_000
        books.apply_event(BookEvent("coinbase", "BTC-USD", "snapshot", [], [(100_000, .02), (100_100, .02)], ts, sequence=1), "USD")
        books.apply_event(BookEvent("kraken", "BTC-USD", "snapshot", [(102_000, .02), (101_900, .02)], [], ts, sequence=1), "USD")
        detector = CandidateDetector(config, books, "test-run", InventoryBook.seeded(config))
        candidates = detector.detect(ts)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertGreater(candidate.expected_profit_usd, 0)
        self.assertAlmostEqual(candidate.quantity, .01)
        self.assertGreater(candidate.estimated_slippage_pct, -1)

    def test_inventory_is_separate_and_does_not_replenish(self):
        config = AppConfig()
        config.enabled_venues = ["coinbase", "kraken"]
        inventory = InventoryBook.seeded(config)
        before = inventory.snapshot()
        candidate = Opportunity(symbol="BTC-USD", base="BTC", buy_venue="coinbase", sell_venue="kraken", buy_quote="USD", sell_quote="USD", quantity=.001, buy_vwap_native=100_000)
        self.assertTrue(inventory.can_buy(candidate, .006))
        self.assertTrue(inventory.can_sell(candidate))
        inventory.apply_buy("coinbase", "BTC", "USD", .001, 100, .6)
        self.assertLess(inventory.balance("coinbase", "USD"), before["coinbase"]["USD"])
        self.assertGreater(inventory.balance("coinbase", "BTC"), before["coinbase"]["BTC"])
        self.assertNotEqual(inventory.snapshot(), before)

    def test_stale_data_gate(self):
        config = AppConfig()
        config.max_market_data_age_ms = 100
        candidate = Opportunity(symbol="BTC-USD", base="BTC", buy_venue="coinbase", sell_venue="kraken", buy_quote="USD", sell_quote="USD", quantity=.001, buy_vwap_native=100_000, expected_net_spread_pct=1, expected_profit_usd=10, estimated_slippage_pct=0, volatility_1s_pct=0, spread_age_ms=1000, market_data_age_ms=101)
        result = common_safety_gate(candidate, InventoryBook.seeded(config), config)
        self.assertIn("stale_market_data", result.reasons)

    def test_simulator_realized_and_partial_fill(self):
        config = AppConfig()
        config.enabled_venues = ["coinbase", "kraken"]
        config.symbols = ["BTC-USD"]
        candidate = Opportunity(symbol="BTC-USD", base="BTC", buy_venue="coinbase", sell_venue="kraken", buy_quote="USD", sell_quote="USD", quantity=.01, buy_best_price=100_000, sell_best_price=102_000, buy_vwap_native=100_000, sell_vwap_native=102_000, expected_profit_usd=8)
        books = BookStore()
        ts = 1_700_000_000_000
        books.apply_event(BookEvent("coinbase", "BTC-USD", "snapshot", [(99_900, .01)], [(100_000, .005)], ts), "USD")
        books.apply_event(BookEvent("kraken", "BTC-USD", "snapshot", [(102_000, .01)], [(102_100, .01)], ts), "USD")
        simulator = ExecutionSimulator(config, lambda venue, symbol, at: books.get(venue, symbol))
        portfolio = InventoryBook.seeded(config)
        result = simulator.simulate(candidate, "baseline", True, ts, ts, portfolio, mutate=True)
        self.assertEqual(result.status, "partial_fill")
        self.assertLess(result.filled_buy_qty, result.filled_sell_qty)
        self.assertNotEqual(result.mark_to_market_pnl_usd, 0.0)

    def test_jev_response_is_typed_and_rejects_invalid_choice(self):
        good = parse_typesafe_response({"model": "jev-1.13.0", "answers": {
            "execution_decision": {"type": "choice", "choice": "EXECUTE", "probabilities": {"EXECUTE": .8, "SKIP": .2}, "confidence": .7},
            "execution_risk": {"type": "choice", "choice": "LOW", "probabilities": {"LOW": 1}, "confidence": 1},
            "spread_persistence": {"type": "choice", "choice": "LIKELY", "probabilities": {"LIKELY": 1}, "confidence": 1},
            "opportunity_quality": {"type": "score", "score": 4, "probabilities": {"3": .2, "4": .8}, "confidence": .8},
        }})
        self.assertEqual(good["execution_decision"], "EXECUTE")
        self.assertAlmostEqual(good["execution_decision_confidence"], .7)
        with self.assertRaises(ValueError):
            bad = json.loads(json.dumps({"model": "jev", "answers": {
                "execution_decision": {"type": "choice", "choice": "MAYBE", "probabilities": {}, "confidence": 0},
                "execution_risk": {"type": "choice", "choice": "LOW", "probabilities": {}, "confidence": 0},
                "spread_persistence": {"type": "choice", "choice": "LIKELY", "probabilities": {}, "confidence": 0},
                "opportunity_quality": {"type": "score", "score": 1, "probabilities": {}, "confidence": 0},
            }}))
            parse_typesafe_response(bad)

    def test_jev_timeout_fails_closed(self):
        class SlowProvider:
            async def decide(self, candidate):
                await asyncio.sleep(0.05)
                return {"execution_decision": "EXECUTE", "execution_decision_probabilities": {"EXECUTE": 1.0}, "execution_decision_confidence": 1.0, "execution_risk": "LOW"}

        config = AppConfig()
        config.jev_timeout_ms = 1
        candidate = Opportunity(symbol="BTC-USD", base="BTC", buy_venue="coinbase", sell_venue="kraken", buy_quote="USD", sell_quote="USD", quantity=.001, buy_vwap_native=100_000, expected_net_spread_pct=1, expected_profit_usd=10, estimated_slippage_pct=0, volatility_1s_pct=0, spread_age_ms=1000, market_data_age_ms=0)
        decision = asyncio.run(JevAssistedStrategy(config, SlowProvider()).evaluate(candidate, InventoryBook.seeded(config)))
        self.assertFalse(decision.accepted)
        self.assertIn("jev_fail_closed", decision.reason)

    def test_replay_has_paired_candidates_and_is_paper_only(self):
        path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        config = AppConfig(db_path=path)
        config.enabled_venues = ["coinbase", "kraken"]
        config.symbols = ["BTC-USD"]
        config.requested_trade_notional_usd = 1000
        config.max_capital_per_opportunity_usd = 1000
        config.min_spread_persistence_ms = 0
        config.min_expected_net_spread_pct = 0
        config.min_expected_profit_usd = 0
        config.cooldown_ms = 0
        db = Database(path)
        runtime = ExperimentRuntime(config, db, mode="replay", provider=ReplayJevProvider())
        events = load_jsonl(str(Path(__file__).resolve().parents[1] / "fixtures" / "sample_session.jsonl"))
        asyncio.run(runtime.run_replay_events(events))
        analytics = db.analytics()
        self.assertEqual(analytics["candidate_opportunities"], 4)
        self.assertEqual(analytics["paired"]["paired_observations"], 4)
        self.assertTrue(analytics["paper_only"])
        self.assertEqual(analytics["run"]["safety_marker"], "PHASE_1_PAPER_ONLY_NO_ORDER_ENDPOINTS")
        self.assertGreater(db.market_snapshot_count(), 0)
        db.close()

    def test_no_order_or_withdrawal_surface_in_runtime(self):
        root = Path(__file__).resolve().parents[1] / "jev_arb"
        source = "\n".join(path.read_text() for path in root.rglob("*.py"))
        self.assertNotIn("def submit_order", source)
        self.assertNotIn("def withdraw", source)


if __name__ == "__main__":
    unittest.main()

