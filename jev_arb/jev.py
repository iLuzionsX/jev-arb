from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .domain import Opportunity, sha256_json


QUESTION_SCHEMA: dict[str, dict[str, Any]] = {
    "execution_decision": {
        "type": "choice",
        "instructions": "Should this already-calculated paper arbitrage opportunity be accepted after considering execution quality and the provided market state?",
        "criteria": {
            "EXECUTE": "Accept the opportunity for paper execution; the edge appears likely to survive the measured latency and market conditions.",
            "SKIP": "Reject the opportunity; the edge appears fragile, low quality, or unlikely to survive execution conditions.",
        },
    },
    "execution_risk": {
        "type": "choice",
        "instructions": "What is the execution risk of this paper arbitrage opportunity?",
        "criteria": {
            "LOW": "Stable, liquid, persistent opportunity with low fill and adverse-movement risk.",
            "MEDIUM": "Some uncertainty or moderate market movement, but not obviously unsafe.",
            "HIGH": "Fragile, stale, thin, rapidly changing, or otherwise high risk of a bad or one-leg fill.",
        },
    },
    "spread_persistence": {
        "type": "choice",
        "instructions": "How likely is the positive net spread to persist until simulated arrival?",
        "criteria": {
            "LIKELY": "Recent spread history and book state support persistence.",
            "UNCERTAIN": "Evidence is mixed or insufficient.",
            "UNLIKELY": "The spread is collapsing, short-lived, or likely to disappear before arrival.",
        },
    },
    "opportunity_quality": {
        "type": "score",
        "instructions": "Rate the overall quality of this paper arbitrage opportunity, considering only the supplied state.",
        "criteria": [
            "1 — poor quality; likely to fail or produce an adverse outcome",
            "2 — weak quality; substantial uncertainty",
            "3 — mixed or average quality",
            "4 — good quality; favorable evidence",
            "5 — exceptional quality; strong evidence of a durable executable edge",
        ],
    },
}


class JevProvider(Protocol):
    async def decide(self, candidate: Opportunity) -> dict[str, Any]:
        ...


class JevUnavailable(RuntimeError):
    pass


@dataclass
class JevCall:
    output: dict[str, Any]
    latency_ms: float
    cached: bool = False


class ResponseCache:
    def __init__(self, path: str):
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def get(self, key: str) -> dict | None:
        return self._data.get(key)

    def put(self, key: str, value: dict) -> None:
        self._data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))


class TypeSafeJevProvider:
    """Official TypeSafe HTTP adapter; no text-generation fallback."""

    def __init__(self, api_key: str | None, endpoint: str, model: str, timeout_ms: int, cache_path: str | None = None):
        self.api_key = api_key or os.getenv("TYPESAFE_API_KEY", "")
        self.endpoint = endpoint
        self.model = model
        self.timeout_ms = timeout_ms
        self.cache = ResponseCache(cache_path) if cache_path else None
        self.available = bool(self.api_key)

    async def decide(self, candidate: Opportunity) -> dict[str, Any]:
        if not self.available:
            raise JevUnavailable("TYPESAFE_API_KEY is not configured")
        state = candidate.jev_state()
        cache_key = sha256_json({"model": self.model, "state": state, "questions": QUESTION_SCHEMA})
        if self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                result = dict(cached)
                result["cached"] = True
                result["cache_key"] = cache_key
                return result
        started = time.perf_counter()
        payload = {"state": state, "model": self.model, "questions": QUESTION_SCHEMA}
        try:
            response_body, response_headers = await asyncio.wait_for(
                asyncio.to_thread(self._post_json, payload), timeout=self.timeout_ms / 1000.0
            )
        except asyncio.TimeoutError as exc:
            raise JevUnavailable(f"Jev timed out after {self.timeout_ms} ms") from exc
        except Exception as exc:
            raise JevUnavailable(str(exc)) from exc
        result = parse_typesafe_response(response_body, response_headers)
        result["cache_key"] = cache_key
        result["cached"] = False
        result["provider_latency_ms"] = (time.perf_counter() - started) * 1000.0
        if self.cache:
            self.cache.put(cache_key, result)
        return result

    def _post_json(self, payload: dict) -> tuple[dict, dict[str, str]]:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "jev-arb/0.1 paper-research",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=max(0.1, self.timeout_ms / 1000.0)) as response:
            body = json.loads(response.read().decode())
            headers = {key.lower(): value for key, value in response.headers.items()}
            return body, headers


def parse_typesafe_response(body: dict, headers: dict[str, str] | None = None) -> dict[str, Any]:
    answers = body.get("answers") or {}
    decision = _choice(answers, "execution_decision")
    risk = _choice(answers, "execution_risk")
    persistence = _choice(answers, "spread_persistence")
    quality = _score(answers, "opportunity_quality")
    if decision["choice"] not in {"EXECUTE", "SKIP"}:
        raise ValueError("Jev returned an invalid execution decision")
    if risk["choice"] not in {"LOW", "MEDIUM", "HIGH"}:
        raise ValueError("Jev returned an invalid execution risk")
    if persistence["choice"] not in {"LIKELY", "UNCERTAIN", "UNLIKELY"}:
        raise ValueError("Jev returned an invalid persistence decision")
    return {
        "model": body.get("model"),
        "request_id": (headers or {}).get("x-typesafe-request-id"),
        "usage": body.get("usage", {}),
        "execution_decision": decision["choice"],
        "execution_decision_probabilities": decision["probabilities"],
        "execution_decision_confidence": decision["confidence"],
        "execution_risk": risk["choice"],
        "execution_risk_probabilities": risk["probabilities"],
        "execution_risk_confidence": risk["confidence"],
        "spread_persistence": persistence["choice"],
        "spread_persistence_probabilities": persistence["probabilities"],
        "spread_persistence_confidence": persistence["confidence"],
        "opportunity_quality": quality["score"],
        "opportunity_quality_probabilities": quality["probabilities"],
        "opportunity_quality_confidence": quality["confidence"],
    }


def _choice(answers: dict, key: str) -> dict[str, Any]:
    answer = answers.get(key)
    if not answer or answer.get("type") != "choice":
        raise ValueError(f"Missing typed Choice answer: {key}")
    return {
        "choice": answer.get("choice"),
        "probabilities": answer.get("probabilities") or {},
        "confidence": float(answer.get("confidence", 0.0)),
    }


def _score(answers: dict, key: str) -> dict[str, Any]:
    answer = answers.get(key)
    if not answer or answer.get("type") != "score":
        raise ValueError(f"Missing typed Score answer: {key}")
    return {
        "score": float(answer.get("score", 0.0)),
        "probabilities": answer.get("probabilities") or {},
        "confidence": float(answer.get("confidence", 0.0)),
    }


class ReplayJevProvider:
    """Deterministic fixture provider; never represents live Jev evidence."""

    def __init__(self, min_execute_spread_pct: float = 0.10):
        self.min_execute_spread_pct = min_execute_spread_pct

    async def decide(self, candidate: Opportunity) -> dict[str, Any]:
        execute = candidate.expected_net_spread_pct >= self.min_execute_spread_pct and candidate.volatility_1s_pct < 0.25
        choice = "EXECUTE" if execute else "SKIP"
        confidence = min(0.99, 0.55 + abs(candidate.expected_net_spread_pct) / 2.0)
        return {
            "model": "replay-jev-fixture",
            "request_id": None,
            "execution_decision": choice,
            "execution_decision_probabilities": {"EXECUTE": confidence if execute else 1.0 - confidence, "SKIP": 1.0 - confidence if execute else confidence},
            "execution_decision_confidence": confidence,
            "execution_risk": "LOW" if execute else "HIGH",
            "execution_risk_probabilities": {},
            "execution_risk_confidence": confidence,
            "spread_persistence": "LIKELY" if execute else "UNLIKELY",
            "spread_persistence_probabilities": {},
            "spread_persistence_confidence": confidence,
            "opportunity_quality": 4.0 if execute else 2.0,
            "opportunity_quality_probabilities": {},
            "opportunity_quality_confidence": confidence,
            "cached": False,
            "provider_latency_ms": 0.01,
        }


class FailClosedJevProvider:
    async def decide(self, candidate: Opportunity) -> dict[str, Any]:
        _ = candidate
        raise JevUnavailable("Jev provider disabled; paper strategy must skip")


