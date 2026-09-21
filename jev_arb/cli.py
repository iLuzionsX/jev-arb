from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .config import AppConfig, pretty_config
from .db import Database
from .jev import ReplayJevProvider, TypeSafeJevProvider
from .replay import load_jsonl
from .runtime import ExperimentRuntime
from .server import DashboardServer


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="jev-arb", description="JEV-ARB Phase 1 public-data paper-trading research")
    sub = parser.add_subparsers(dest="command", required=True)

    live = sub.add_parser("live", help="run a public read-only live paper session")
    live.add_argument("--duration-seconds", type=float, default=60.0)
    live.add_argument("--db", default=os.getenv("JEV_ARB_DB", "data/live.db"))
    live.add_argument("--host", default="127.0.0.1")
    live.add_argument("--port", type=int, default=8000)
    live.add_argument("--no-server", action="store_true")
    live.add_argument("--venues", default=None, help="comma-separated public venues")

    replay = sub.add_parser("replay", help="replay a recorded JSONL market session")
    replay.add_argument("--input", required=True)
    replay.add_argument("--db", default="data/replay.db")
    replay.add_argument("--jev-mode", choices=["fixture", "live", "fail-closed"], default="fixture")

    demo = sub.add_parser("demo", help="run the checked-in synthetic fixture")
    demo.add_argument("--db", default="data/demo.db")

    serve = sub.add_parser("serve", help="serve a completed database dashboard")
    serve.add_argument("--db", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    report = sub.add_parser("report", help="print measured analytics and Is there an edge?")
    report.add_argument("--db", required=True)

    export = sub.add_parser("export", help="export JSON and CSV analysis")
    export.add_argument("--db", required=True)
    export.add_argument("--out", default="data/exports")

    args = parser.parse_args(argv)
    if args.command == "live":
        return asyncio.run(run_live(args))
    if args.command == "replay":
        return asyncio.run(run_replay(args))
    if args.command == "demo":
        return asyncio.run(run_demo(args))
    if args.command == "serve":
        return run_serve(args)
    if args.command == "report":
        return run_report(args)
    if args.command == "export":
        return run_export(args)
    return 2


async def run_live(args) -> int:
    config = AppConfig.from_env(args.db)
    if args.venues:
        config.enabled_venues = [venue.strip() for venue in args.venues.split(",") if venue.strip()]
    db = Database(config.db_path)
    runtime = ExperimentRuntime(config, db, mode="live")
    server = None if args.no_server else DashboardServer(db, runtime, args.host, args.port)
    if server:
        server.start()
        print(f"Dashboard: http://{args.host}:{args.port}")
    print("PAPER TRADING — NO REAL ORDERS")
    print(f"Live public market-data session for {args.duration_seconds:g}s; DB={config.db_path}")
    try:
        await runtime.run_live(args.duration_seconds)
    finally:
        if server:
            server.stop()
        db.close()
    return 0


async def run_replay(args) -> int:
    config = AppConfig.from_env(args.db)
    config.enabled_venues = ["coinbase", "kraken"]
    config.symbols = ["BTC-USD"]
    config.requested_trade_notional_usd = 1_000.0
    config.max_capital_per_opportunity_usd = 1_000.0
    config.reference_prices_usd["BTC-USD"] = 100_000.0
    config.min_spread_persistence_ms = 0
    config.min_expected_net_spread_pct = 0.0
    config.min_expected_profit_usd = 0.0
    config.cooldown_ms = 0
    provider = provider_for(args.jev_mode, config)
    db = Database(config.db_path)
    runtime = ExperimentRuntime(config, db, mode="replay", provider=provider)
    try:
        await runtime.run_replay_events(load_jsonl(args.input))
    finally:
        db.close()
    report_db = Database(config.db_path)
    print(json.dumps(report_db.analytics(), indent=2, default=str))
    report_db.close()
    return 0


async def run_demo(args) -> int:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "sample_session.jsonl"
    replay_args = type("Args", (), {"input": str(fixture), "db": args.db, "jev_mode": "fixture"})
    return await run_replay(replay_args)


def provider_for(mode: str, config: AppConfig):
    if mode == "fixture":
        return ReplayJevProvider()
    if mode == "live":
        return TypeSafeJevProvider(os.getenv("TYPESAFE_API_KEY"), config.jev_endpoint, config.jev_model, config.jev_timeout_ms, config.jev_cache_path)
    from .jev import FailClosedJevProvider
    return FailClosedJevProvider()


def run_serve(args) -> int:
    db = Database(args.db)
    server = DashboardServer(db, None, args.host, args.port)
    server.start()
    print(f"Dashboard: http://{args.host}:{args.port}")
    print("Press Ctrl-C to stop. PAPER TRADING — NO REAL ORDERS")
    try:
        while True:
            asyncio.run(asyncio.sleep(3600))
    except KeyboardInterrupt:
        server.stop()
        db.close()
    return 0


def run_report(args) -> int:
    db = Database(args.db)
    analytics = db.analytics()
    print(json.dumps(analytics, indent=2, default=str))
    baseline = analytics["strategies"]["baseline"]
    jev = analytics["strategies"]["jev"]
    paired = analytics["paired"]
    run = analytics.get("run") or {}
    print("\nIs there an edge?")
    print(f"number of opportunities observed: {analytics['candidate_opportunities']}")
    print(f"observation duration: {analytics['observation_duration_seconds']:.3f}s")
    print(f"simulated capital: {((run.get('config') or {}).get('paper_capital_usd', 'unknown'))}")
    print(f"baseline net P&L: ${baseline['net_simulated_pnl_usd']:.4f}")
    print(f"Jev net P&L: ${jev['net_simulated_pnl_usd']:.4f}")
    print(f"Jev incremental P&L: ${paired['jev_incremental_pnl_usd']:.4f}")
    print(f"Jev latency p50/p95/p99: {jev['latency_ms']}")
    print(f"confidence calibration buckets: {jev['confidence_buckets']}")
    print(f"statistically meaningful yet: {analytics['statistical_conclusion']}")
    db.close()
    return 0


def run_export(args) -> int:
    db = Database(args.db)
    paths = db.export_json(args.out) + db.export_csv(args.out)
    print("\n".join(paths))
    db.close()
    return 0


def load_dotenv() -> None:
    path = Path(".env")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())

