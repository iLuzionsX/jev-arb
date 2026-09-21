from __future__ import annotations

import csv
import json
import math
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import SAFETY_MARKER
from .config import AppConfig, ensure_parent
from .domain import Decision, Opportunity, TradeResult, now_ms, stable_json


class Database:
    def __init__(self, path: str):
        ensure_parent(path)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def init_schema(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schema.sql"
        schema = schema_path.read_text() if schema_path.exists() else _fallback_schema()
        with self._lock:
            self.conn.executescript(schema)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def start_run(self, config: AppConfig, mode: str, notes: str = "") -> str:
        run_id = f"{mode}-{int(time.time())}-{now_ms() % 1000:03d}"
        with self._lock:
            self.conn.execute(
                "INSERT INTO experiment_runs(run_id,started_at_ms,mode,config_json,safety_marker,notes) VALUES (?,?,?,?,?,?)",
                (run_id, now_ms(), mode, json.dumps(config.to_dict(), sort_keys=True), SAFETY_MARKER, notes),
            )
            self.conn.commit()
        return run_id

    def finish_run(self, run_id: str, ended_at_ms: int | None = None) -> None:
        with self._lock:
            self.conn.execute("UPDATE experiment_runs SET ended_at_ms=? WHERE run_id=?", (ended_at_ms or now_ms(), run_id))
            self.conn.commit()

    def set_run_window(self, run_id: str, started_at_ms: int, ended_at_ms: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE experiment_runs SET started_at_ms=?, ended_at_ms=? WHERE run_id=?",
                (started_at_ms, ended_at_ms, run_id),
            )
            self.conn.commit()

    def record_snapshot(self, run_id: str, snapshot: dict, ts_ms: int | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO market_snapshots(run_id,ts_ms,venue,symbol,sequence,data_age_ms,book_json) VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    ts_ms or now_ms(),
                    snapshot["venue"],
                    snapshot["symbol"],
                    snapshot.get("sequence"),
                    max(0, (ts_ms or now_ms()) - int(snapshot.get("updated_at_ms", ts_ms or now_ms()))),
                    stable_json(snapshot),
                ),
            )
            self.conn.commit()

    def record_health(self, run_id: str, venue: str, health: dict) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO exchange_health(run_id,venue,connected,last_message_ms,reconnects,last_error,updated_at_ms)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(run_id,venue) DO UPDATE SET
                     connected=excluded.connected,last_message_ms=excluded.last_message_ms,
                     reconnects=excluded.reconnects,last_error=excluded.last_error,updated_at_ms=excluded.updated_at_ms""",
                (
                    run_id, venue, int(bool(health.get("connected"))), health.get("last_message_ms"),
                    int(health.get("reconnects", 0)), health.get("last_error"), now_ms(),
                ),
            )
            self.conn.commit()

    def record_candidate(self, candidate: Opportunity) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT OR IGNORE INTO candidates(candidate_id,run_id,detected_at_ms,symbol,buy_venue,sell_venue,
                   expected_net_profit_usd,expected_net_spread_pct,candidate_json) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    candidate.candidate_id, candidate.run_id, candidate.detected_at_ms, candidate.symbol,
                    candidate.buy_venue, candidate.sell_venue, candidate.expected_profit_usd,
                    candidate.expected_net_spread_pct, stable_json(candidate.to_dict()),
                ),
            )
            self.conn.commit()

    def record_decision(self, decision: Decision) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO decisions(decision_id,candidate_id,strategy,decision,reason,accepted,
                   decision_at_ms,latency_ms,confidence,input_json,output_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision.decision_id, decision.candidate_id, decision.strategy, decision.decision,
                    decision.reason, int(decision.accepted), decision.decision_at_ms, decision.latency_ms,
                    decision.confidence, stable_json(decision.input_state), stable_json(decision.output),
                ),
            )
            self.conn.commit()

    def record_trade(self, trade: TradeResult) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO simulated_trades(trade_id,candidate_id,strategy,accepted,status,
                   detection_at_ms,decision_at_ms,arrival_at_ms,expected_pnl_usd,realized_pnl_usd,
                   mark_to_market_pnl_usd,net_cashflow_usd,filled_buy_qty,filled_sell_qty,trade_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade.trade_id, trade.candidate_id, trade.strategy, int(trade.accepted), trade.status,
                    trade.detection_at_ms, trade.decision_at_ms, trade.arrival_at_ms, trade.expected_pnl_usd,
                    trade.realized_pnl_usd, trade.mark_to_market_pnl_usd, trade.net_cashflow_usd,
                    trade.filled_buy_qty, trade.filled_sell_qty, stable_json(trade.to_dict()),
                ),
            )
            if trade.buy_fill:
                self.record_fill(trade.trade_id, trade.buy_fill.to_dict())
            if trade.sell_fill:
                self.record_fill(trade.trade_id, trade.sell_fill.to_dict())
            self.conn.commit()

    def record_fill(self, trade_id: str, fill: dict) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO fills(fill_id,trade_id,venue,side,base_qty,quote_amount,vwap,fee_usd,fill_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                fill["fill_id"], trade_id, fill["venue"], fill["side"], fill["base_qty"],
                fill["quote_amount"], fill["vwap"], fill["fee_usd"], stable_json(fill),
            ),
        )

    def record_inventory(self, run_id: str, strategy: str, inventory: dict, ts_ms: int | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO inventory_snapshots(run_id,ts_ms,strategy,inventory_json) VALUES(?,?,?,?)",
                (run_id, ts_ms or now_ms(), strategy, stable_json(inventory)),
            )
            self.conn.commit()

    def list_candidates(self, run_id: str | None = None, limit: int = 100) -> list[dict]:
        query = "SELECT * FROM candidates"
        params: list[Any] = []
        if run_id:
            query += " WHERE run_id=?"
            params.append(run_id)
        query += " ORDER BY detected_at_ms DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
            result = []
            for row in rows:
                item = json.loads(row["candidate_json"])
                item["decisions"] = self._decisions_for(row["candidate_id"])
                item["trades"] = self._trades_for(row["candidate_id"])
                result.append(item)
            return result

    def candidate_detail(self, candidate_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if not row:
                return None
            item = json.loads(row["candidate_json"])
            item["decisions"] = self._decisions_for(candidate_id)
            item["trades"] = self._trades_for(candidate_id)
            return item

    def _decisions_for(self, candidate_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM decisions WHERE candidate_id=? ORDER BY decision_at_ms", (candidate_id,)).fetchall()
        return [
            {
                "decision_id": row["decision_id"], "candidate_id": candidate_id, "strategy": row["strategy"],
                "decision": row["decision"], "reason": row["reason"], "accepted": bool(row["accepted"]),
                "decision_at_ms": row["decision_at_ms"], "latency_ms": row["latency_ms"],
                "confidence": row["confidence"], "input": json.loads(row["input_json"]),
                "output": json.loads(row["output_json"]),
            }
            for row in rows
        ]

    def _trades_for(self, candidate_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM simulated_trades WHERE candidate_id=? ORDER BY arrival_at_ms", (candidate_id,)).fetchall()
        return [json.loads(row["trade_json"]) for row in rows]

    def current_run(self) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM experiment_runs ORDER BY started_at_ms DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def market_snapshot_count(self, run_id: str | None = None) -> int:
        with self._lock:
            if run_id:
                return int(self.conn.execute("SELECT COUNT(*) FROM market_snapshots WHERE run_id=?", (run_id,)).fetchone()[0])
            return int(self.conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0])

    def replay_snapshots(self, run_id: str | None = None) -> list[tuple[int, dict]]:
        with self._lock:
            query = "SELECT ts_ms,book_json FROM market_snapshots"
            params: list[Any] = []
            if run_id:
                query += " WHERE run_id=?"
                params.append(run_id)
            query += " ORDER BY ts_ms,id"
            return [(int(row["ts_ms"]), json.loads(row["book_json"])) for row in self.conn.execute(query, params)]

    def analytics(self, run_id: str | None = None) -> dict:
        with self._lock:
            run = self.current_run() if run_id is None else self._run(run_id)
            params: list[Any] = []
            where = ""
            if run_id:
                where = " WHERE c.run_id=?"
                params.append(run_id)
            candidate_count = int(self.conn.execute(f"SELECT COUNT(*) FROM candidates c{where}", params).fetchone()[0])
            snapshots = self.market_snapshot_count(run_id)
            strategies = {name: self._strategy_metrics(name, run_id) for name in ("baseline", "jev")}
            paired = self._paired_metrics(run_id)
            opportunity_metrics = self._opportunity_metrics(run_id)
            duration_ms = 0
            if run:
                started = int(run["started_at_ms"])
                ended = int(run["ended_at_ms"] or now_ms())
                duration_ms = max(0, ended - started)
            return {
                "run": _row_to_json(run),
                "duration_ms": duration_ms,
                "observation_duration_seconds": duration_ms / 1000.0,
                "candidate_opportunities": candidate_count,
                "opportunities_per_hour": candidate_count / (duration_ms / 3_600_000.0) if duration_ms > 0 else 0.0,
                "market_snapshot_count": snapshots,
                "opportunities": opportunity_metrics,
                "strategies": strategies,
                "paired": paired,
                "paper_only": True,
                "statistical_conclusion": _statistical_conclusion(candidate_count, paired),
            }

    def _opportunity_metrics(self, run_id: str | None) -> dict:
        params: list[Any] = []
        where = ""
        if run_id:
            where = " WHERE run_id=?"
            params.append(run_id)
        rows = self.conn.execute(f"SELECT candidate_json FROM candidates{where}", params).fetchall()
        items = []
        for row in rows:
            try:
                items.append(json.loads(row["candidate_json"]))
            except (TypeError, json.JSONDecodeError):
                continue
        raw_spreads = [float(item.get("gross_spread_pct", 0.0)) for item in items]
        net_spreads = [float(item.get("expected_net_spread_pct", 0.0)) for item in items]
        lifetimes = [float(item.get("spread_age_ms", 0.0)) for item in items]
        depths = [min(float(item.get("buy_depth_usd", 0.0)), float(item.get("sell_depth_usd", 0.0))) for item in items]
        assets: dict[str, int] = {}
        routes: dict[str, int] = {}
        for item in items:
            assets[item.get("symbol", "unknown")] = assets.get(item.get("symbol", "unknown"), 0) + 1
            route = f"{item.get('buy_venue', 'unknown')}/{item.get('sell_venue', 'unknown')}"
            routes[route] = routes.get(route, 0) + 1
        return {
            "total_candidate_opportunities": len(items),
            "opportunities_per_hour": self._opportunities_per_hour(run_id, len(items)),
            "raw_spread_distribution_pct": _distribution(raw_spreads),
            "net_spread_distribution_pct": _distribution(net_spreads),
            "opportunity_lifetime_ms": _distribution(lifetimes),
            "executable_depth_usd": _distribution(depths),
            "assets": assets,
            "exchange_pairs": routes,
        }

    def _opportunities_per_hour(self, run_id: str | None, count: int) -> float:
        run = self.current_run() if not run_id else self._run(run_id)
        if not run:
            return 0.0
        duration = max(0, int(run["ended_at_ms"] or now_ms()) - int(run["started_at_ms"]))
        return count / (duration / 3_600_000.0) if duration else 0.0

    def _run(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM experiment_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def _strategy_metrics(self, strategy: str, run_id: str | None) -> dict:
        params: list[Any] = [strategy]
        where = "WHERE strategy=?"
        if run_id:
            where += " AND candidate_id IN (SELECT candidate_id FROM candidates WHERE run_id=?)"
            params.append(run_id)
        decision_rows = self.conn.execute(f"SELECT * FROM decisions {where}", params).fetchall()
        trade_rows = self.conn.execute(f"SELECT * FROM simulated_trades {where}", params).fetchall()
        accepted = sum(1 for row in decision_rows if row["accepted"])
        rejected = sum(1 for row in decision_rows if not row["accepted"])
        executed = [row for row in trade_rows if row["accepted"]]
        counterfactual_rejects = [row for row in trade_rows if not row["accepted"]]
        pnls = [float(row["realized_pnl_usd"]) for row in executed]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        positive_rejects = sum(1 for row in counterfactual_rejects if float(row["realized_pnl_usd"]) > 0)
        negative_rejects = sum(1 for row in counterfactual_rejects if float(row["realized_pnl_usd"]) < 0)
        latencies = [float(row["latency_ms"]) for row in decision_rows if row["latency_ms"] is not None]
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in pnls:
            cumulative += pnl
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
        confidence = self._confidence_buckets(strategy, run_id)
        statuses: dict[str, int] = {}
        for row in trade_rows:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        gross = sum(pnls)
        profit_factor = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0.0)
        return {
            "accepted_opportunities": accepted,
            "rejected_opportunities": rejected,
            "simulated_trades": len(executed),
            "profitable_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": len(wins) / len(pnls) if pnls else 0.0,
            "gross_simulated_pnl_usd": gross,
            "net_simulated_pnl_usd": gross,
            "average_pnl_per_trade_usd": statistics.mean(pnls) if pnls else 0.0,
            "median_pnl_per_trade_usd": statistics.median(pnls) if pnls else 0.0,
            "average_return_per_trade_pct": self._average_return(strategy, run_id),
            "profit_factor": profit_factor,
            "max_drawdown_usd": max_drawdown,
            "capital_utilization_pct": self._capital_utilization(strategy, run_id),
            "profit_per_1000_deployed_usd": self._profit_per_1000(strategy, run_id),
            "pnl_by_exchange_pair": self._group_pnl(strategy, run_id, "buy_venue", "sell_venue"),
            "pnl_by_asset": self._group_pnl(strategy, run_id, "symbol"),
            "pnl_by_market_regime": self._pnl_by_regime(strategy, run_id),
            "status_counts": statuses,
            "decision_distribution": {
                "EXECUTE": accepted,
                "SKIP": rejected,
            },
            "accepted_opportunities_that_lost_money": sum(1 for pnl in pnls if pnl < 0),
            "expected_vs_realized_delta_usd": statistics.mean(
                [float(row["expected_pnl_usd"]) - float(row["realized_pnl_usd"]) for row in executed]
            ) if executed else 0.0,
            "rejected_opportunities_that_would_have_made_money": positive_rejects,
            "rejected_opportunities_that_would_have_lost_money": negative_rejects,
            "latency_ms": _percentiles(latencies),
            "confidence_buckets": confidence,
        }

    def _average_return(self, strategy: str, run_id: str | None) -> float:
        rows = self._trade_rows(strategy, run_id, accepted_only=True)
        returns = []
        for row in rows:
            candidate = self.conn.execute("SELECT candidate_json FROM candidates WHERE candidate_id=?", (row["candidate_id"],)).fetchone()
            if candidate:
                try:
                    notional = float(json.loads(candidate["candidate_json"]).get("requested_notional_usd", 0.0))
                    if notional:
                        returns.append(float(row["realized_pnl_usd"]) / notional * 100.0)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
        return statistics.mean(returns) if returns else 0.0

    def _capital_utilization(self, strategy: str, run_id: str | None) -> float:
        rows = self._trade_rows(strategy, run_id, accepted_only=True)
        if not rows:
            return 0.0
        run = self.current_run() if not run_id else self._run(run_id)
        capital = 25_000.0
        if run:
            try:
                capital = float(json.loads(run["config_json"]).get("paper_capital_usd", capital))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        notionals = []
        for row in rows:
            candidate = self.conn.execute("SELECT candidate_json FROM candidates WHERE candidate_id=?", (row["candidate_id"],)).fetchone()
            if candidate:
                try:
                    notionals.append(float(json.loads(candidate["candidate_json"]).get("requested_notional_usd", 0.0)))
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
        return statistics.mean(notionals) / capital * 100.0 if notionals and capital else 0.0

    def _profit_per_1000(self, strategy: str, run_id: str | None) -> float:
        run = self.current_run() if not run_id else self._run(run_id)
        capital = 25_000.0
        if run:
            try:
                capital = float(json.loads(run["config_json"]).get("paper_capital_usd", capital))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        pnl = sum(float(row["realized_pnl_usd"]) for row in self._trade_rows(strategy, run_id, accepted_only=True))
        return pnl / capital * 1000.0 if capital else 0.0

    def _group_pnl(self, strategy: str, run_id: str | None, *fields: str) -> dict:
        rows = self._trade_rows(strategy, run_id, accepted_only=True)
        result: dict[str, float] = {}
        for row in rows:
            item = json.loads(row["trade_json"])
            candidate = self.candidate_detail(row["candidate_id"]) or {}
            key_values = [str(candidate.get(field, item.get(field, "unknown"))) for field in fields]
            key = "/".join(key_values)
            result[key] = result.get(key, 0.0) + float(row["realized_pnl_usd"])
        return result

    def _pnl_by_regime(self, strategy: str, run_id: str | None) -> dict:
        rows = self._trade_rows(strategy, run_id, accepted_only=True)
        result: dict[str, float] = {}
        for row in rows:
            candidate = self.candidate_detail(row["candidate_id"]) or {}
            regime = _market_regime(candidate)
            result[regime] = result.get(regime, 0.0) + float(row["realized_pnl_usd"])
        return result

    def _trade_rows(self, strategy: str, run_id: str | None, accepted_only: bool = False) -> list[sqlite3.Row]:
        params: list[Any] = [strategy]
        where = "WHERE strategy=?"
        if accepted_only:
            where += " AND accepted=1"
        if run_id:
            where += " AND candidate_id IN (SELECT candidate_id FROM candidates WHERE run_id=?)"
            params.append(run_id)
        return self.conn.execute(f"SELECT * FROM simulated_trades {where}", params).fetchall()

    def _confidence_buckets(self, strategy: str, run_id: str | None) -> dict[str, dict]:
        params: list[Any] = [strategy]
        where = "WHERE d.strategy=? AND d.confidence IS NOT NULL"
        if run_id:
            where += " AND d.candidate_id IN (SELECT candidate_id FROM candidates WHERE run_id=?)"
            params.append(run_id)
        rows = self.conn.execute(
            f"""SELECT d.confidence,d.candidate_id,t.realized_pnl_usd,t.accepted,t.status
                FROM decisions d LEFT JOIN simulated_trades t
                ON t.candidate_id=d.candidate_id AND t.strategy=d.strategy {where}""", params
        ).fetchall()
        buckets: dict[str, list[float]] = {}
        for row in rows:
            confidence = float(row["confidence"])
            lower = math.floor(confidence * 10) / 10
            key = f"{lower:.1f}-{min(1.0, lower + 0.1):.1f}"
            buckets.setdefault(key, []).append(float(row["realized_pnl_usd"] or 0.0))
        return {key: {"count": len(values), "avg_realized_pnl_usd": statistics.mean(values) if values else 0.0,
                      "profitable_rate": sum(1 for value in values if value > 0) / len(values) if values else 0.0}
                for key, values in sorted(buckets.items())}

    def _paired_metrics(self, run_id: str | None) -> dict:
        params: list[Any] = []
        where = ""
        if run_id:
            where = "WHERE c.run_id=?"
            params.append(run_id)
        rows = self.conn.execute(
            f"""SELECT c.candidate_id,
                      b.realized_pnl_usd AS baseline_pnl,
                      j.realized_pnl_usd AS jev_pnl,
                      b.status AS baseline_status,
                      j.status AS jev_status
                FROM candidates c
                LEFT JOIN simulated_trades b ON b.candidate_id=c.candidate_id AND b.strategy='baseline'
                LEFT JOIN simulated_trades j ON j.candidate_id=c.candidate_id AND j.strategy='jev'
                {where}""", params
        ).fetchall()
        diffs = [float(row["jev_pnl"] or 0) - float(row["baseline_pnl"] or 0) for row in rows if row["baseline_pnl"] is not None and row["jev_pnl"] is not None]
        jev_positive = sum(1 for row in rows if float(row["jev_pnl"] or 0) > 0 and float(row["baseline_pnl"] or 0) <= 0)
        baseline_positive = sum(1 for row in rows if float(row["baseline_pnl"] or 0) > 0 and float(row["jev_pnl"] or 0) <= 0)
        mean = statistics.mean(diffs) if diffs else 0.0
        stdev = statistics.stdev(diffs) if len(diffs) >= 2 else 0.0
        se = stdev / math.sqrt(len(diffs)) if diffs else 0.0
        z = mean / se if se else 0.0
        p = math.erfc(abs(z) / math.sqrt(2)) if se else None
        latency_lost = sum(
            1 for row in rows
            if row["baseline_pnl"] is not None and row["jev_pnl"] is not None
            and float(row["baseline_pnl"]) > 0 and float(row["jev_pnl"]) <= 0
            and row["baseline_status"] == "executed"
            and row["jev_status"] in {"executed", "partial_fill", "missed_opportunity", "one_leg_fill", "counterfactual_rejected", "counterfactual_partial_rejected", "counterfactual_one_leg_rejected"}
        )
        return {
            "paired_observations": len(diffs),
            "jev_incremental_pnl_usd": mean * len(diffs),
            "mean_paired_difference_usd": mean,
            "paired_difference_stddev_usd": stdev,
            "paired_difference_p_value_approx": p,
            "jev_wins_over_baseline_count": jev_positive,
            "baseline_wins_over_jev_count": baseline_positive,
            "jev_latency_cost_observations": latency_lost,
            "opportunities_lost_because_jev_latency": latency_lost,
        }

    def export_json(self, out_dir: str, run_id: str | None = None) -> list[str]:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        payload = {
            "analytics": self.analytics(run_id),
            "candidates": self.list_candidates(run_id, limit=100_000),
        }
        path = str(Path(out_dir) / "jev-arb-export.json")
        Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True))
        return [path]

    def export_csv(self, out_dir: str, run_id: str | None = None) -> list[str]:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        params: list[Any] = []
        query = """SELECT c.candidate_id,c.detected_at_ms,c.symbol,c.buy_venue,c.sell_venue,
                   c.expected_net_profit_usd,c.expected_net_spread_pct,
                   d.strategy,d.decision,d.reason,d.accepted,d.latency_ms,d.confidence,
                   t.status,t.realized_pnl_usd,t.mark_to_market_pnl_usd,t.expected_pnl_usd
                   FROM candidates c LEFT JOIN decisions d ON d.candidate_id=c.candidate_id
                   LEFT JOIN simulated_trades t ON t.candidate_id=c.candidate_id AND t.strategy=d.strategy"""
        if run_id:
            query += " WHERE c.run_id=?"
            params.append(run_id)
        query += " ORDER BY c.detected_at_ms"
        path = str(Path(out_dir) / "jev-arb-opportunities.csv")
        with self._lock, open(path, "w", newline="") as handle:
            rows = self.conn.execute(query, params).fetchall()
            writer = csv.writer(handle)
            writer.writerow(rows[0].keys() if rows else ["candidate_id"])
            for row in rows:
                writer.writerow(list(row))
        return [path]


def _row_to_json(row: dict | sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    if "config_json" in result:
        result["config"] = json.loads(result["config_json"])
    return result


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None}
    ordered = sorted(values)
    def percentile(q: float) -> float:
        index = min(len(ordered) - 1, max(0, int(math.ceil(q * len(ordered))) - 1))
        return ordered[index]
    return {"p50": percentile(0.50), "p95": percentile(0.95), "p99": percentile(0.99)}


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p50": ordered[(len(ordered) - 1) // 2],
        "p95": ordered[min(len(ordered) - 1, int(math.ceil(len(ordered) * 0.95)) - 1)],
        "max": ordered[-1],
    }


def _market_regime(candidate: dict) -> str:
    volatility = float(candidate.get("volatility_1s_pct", 0.0))
    net_spread = float(candidate.get("expected_net_spread_pct", 0.0))
    if volatility >= 0.25:
        return "volatile"
    if net_spread >= 0.25:
        return "wide_spread"
    return "stable_thin_edge"


def _statistical_conclusion(count: int, paired: dict) -> str:
    if count < 30:
        return "Insufficient observations for a meaningful conclusion."
    p = paired.get("paired_difference_p_value_approx")
    if p is None:
        return "Insufficient variation for a meaningful conclusion."
    return "Paired difference is statistically significant at the approximate 5% level." if p < 0.05 else "No statistically meaningful difference detected at the approximate 5% level."


def _fallback_schema() -> str:
    return """
    CREATE TABLE IF NOT EXISTS experiment_runs(run_id TEXT PRIMARY KEY,started_at_ms INTEGER NOT NULL,ended_at_ms INTEGER,mode TEXT NOT NULL,config_json TEXT NOT NULL,safety_marker TEXT NOT NULL,notes TEXT);
    CREATE TABLE IF NOT EXISTS market_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,ts_ms INTEGER,venue TEXT,symbol TEXT,sequence INTEGER,data_age_ms INTEGER,book_json TEXT);
    CREATE TABLE IF NOT EXISTS exchange_health(run_id TEXT,venue TEXT,connected INTEGER,last_message_ms INTEGER,reconnects INTEGER,last_error TEXT,updated_at_ms INTEGER,PRIMARY KEY(run_id,venue));
    CREATE TABLE IF NOT EXISTS candidates(candidate_id TEXT PRIMARY KEY,run_id TEXT,detected_at_ms INTEGER,symbol TEXT,buy_venue TEXT,sell_venue TEXT,expected_net_profit_usd REAL,expected_net_spread_pct REAL,candidate_json TEXT);
    CREATE TABLE IF NOT EXISTS decisions(decision_id TEXT PRIMARY KEY,candidate_id TEXT,strategy TEXT,decision TEXT,reason TEXT,accepted INTEGER,decision_at_ms INTEGER,latency_ms REAL,confidence REAL,input_json TEXT,output_json TEXT);
    CREATE TABLE IF NOT EXISTS simulated_trades(trade_id TEXT PRIMARY KEY,candidate_id TEXT,strategy TEXT,accepted INTEGER,status TEXT,detection_at_ms INTEGER,decision_at_ms INTEGER,arrival_at_ms INTEGER,expected_pnl_usd REAL,realized_pnl_usd REAL,mark_to_market_pnl_usd REAL,net_cashflow_usd REAL,filled_buy_qty REAL,filled_sell_qty REAL,trade_json TEXT);
    CREATE TABLE IF NOT EXISTS fills(fill_id TEXT PRIMARY KEY,trade_id TEXT,venue TEXT,side TEXT,base_qty REAL,quote_amount REAL,vwap REAL,fee_usd REAL,fill_json TEXT);
    CREATE TABLE IF NOT EXISTS inventory_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,ts_ms INTEGER,strategy TEXT,inventory_json TEXT);
    """

