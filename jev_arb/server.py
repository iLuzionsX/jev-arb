from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import SAFETY_MARKER
from .db import Database
from .runtime import ExperimentRuntime


class DashboardServer:
    def __init__(self, db: Database, runtime: ExperimentRuntime | None = None, host: str = "127.0.0.1", port: int = 8000):
        self.db = db
        self.runtime = runtime
        self.host = host
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        state = DashboardState(self.db, self.runtime)
        handler = type("JevArbHandler", (_Handler,), {"state": state})
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="jev-arb-dashboard", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2)


class DashboardState:
    def __init__(self, db: Database, runtime: ExperimentRuntime | None):
        self.db = db
        self.runtime = runtime

    def health(self) -> dict:
        return {
            "paper_only": True,
            "safety_marker": SAFETY_MARKER,
            "run": self.db.current_run(),
            "exchanges": self.runtime.books.health() if self.runtime else {},
        }

    def current_books(self) -> list[dict]:
        if not self.runtime:
            return []
        books = []
        for book in self.runtime.books.all_current():
            bid = book.best_bid()
            ask = book.best_ask()
            books.append({
                "venue": book.venue,
                "symbol": book.symbol,
                "bid": bid.price if bid else None,
                "ask": ask.price if ask else None,
                "age_ms": book.age_ms(),
                "sequence": book.sequence,
            })
        return books

    def inventories(self) -> dict:
        if not self.runtime:
            return {}
        return {"baseline": self.runtime.baseline_inventory.snapshot(), "jev": self.runtime.jev_inventory.snapshot()}

    def overview(self) -> dict:
        analytics = self.db.analytics()
        return {
            **analytics,
            "health": self.health(),
            "books": self.current_books(),
            "inventories": self.inventories(),
        }


class _Handler(BaseHTTPRequestHandler):
    state: DashboardState
    static_root = Path(__file__).resolve().parents[1] / "dashboard"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._api(parsed.path, parse_qs(parsed.query))
            return
        path = "index.html" if parsed.path in {"", "/"} else parsed.path.removeprefix("/")
        if path not in {"index.html", "app.js", "styles.css"}:
            self.send_error(404)
            return
        file_path = self.static_root / path
        if not file_path.exists():
            self.send_error(404)
            return
        body = file_path.read_bytes()
        content_type = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "styles.css": "text/css; charset=utf-8"}[path]
        self._send(body, content_type)

    def _api(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health":
            self._json(self.state.health())
        elif path == "/api/overview":
            self._json(self.state.overview())
        elif path == "/api/analytics":
            self._json(self.state.db.analytics())
        elif path == "/api/candidates":
            limit = min(500, max(1, int(query.get("limit", [100])[0])))
            self._json({"candidates": self.state.db.list_candidates(limit=limit)})
        elif path.startswith("/api/candidates/"):
            candidate_id = path.rsplit("/", 1)[-1]
            detail = self.state.db.candidate_detail(candidate_id)
            if detail is None:
                self.send_error(404)
            else:
                self._json(detail)
        else:
            self.send_error(404)

    def _json(self, value: dict) -> None:
        self._send(json.dumps(value, default=str).encode(), "application/json; charset=utf-8")

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


