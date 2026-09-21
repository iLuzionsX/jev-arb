from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .domain import Decision, Opportunity, now_ms
from .inventory import InventoryBook
from .jev import JevProvider, JevUnavailable


@dataclass
class GateResult:
    reasons: list[str]

    @property
    def ok(self) -> bool:
        return not self.reasons


def common_safety_gate(candidate: Opportunity, portfolio: InventoryBook, config: AppConfig) -> GateResult:
    reasons: list[str] = []
    if candidate.market_data_age_ms > config.max_market_data_age_ms:
        reasons.append("stale_market_data")
    if candidate.expected_net_spread_pct < config.min_expected_net_spread_pct:
        reasons.append("net_spread_below_threshold")
    if candidate.expected_profit_usd < config.min_expected_profit_usd:
        reasons.append("expected_profit_below_threshold")
    if candidate.estimated_slippage_pct > config.max_slippage_pct:
        reasons.append("slippage_above_threshold")
    if candidate.volatility_1s_pct > config.max_volatility_1s_pct:
        reasons.append("volatility_above_threshold")
    if candidate.spread_age_ms < config.min_spread_persistence_ms:
        reasons.append("spread_not_persistent")
    if candidate.requested_notional_usd > config.max_capital_per_opportunity_usd:
        reasons.append("capital_per_opportunity_limit")
    inventory_ok, inventory_reasons = portfolio.can_execute(
        candidate, config.venues()[candidate.buy_venue].taker_fee_pct
    )
    if not inventory_ok:
        reasons.extend(inventory_reasons)
    return GateResult(reasons)


class DeterministicStrategy:
    name = "baseline"

    def __init__(self, config: AppConfig):
        self.config = config

    async def evaluate(self, candidate: Opportunity, portfolio: InventoryBook) -> Decision:
        started = time.perf_counter()
        gates = common_safety_gate(candidate, portfolio, self.config)
        decision = "EXECUTE" if gates.ok else "SKIP"
        reason = "deterministic_all_gates_passed" if gates.ok else ";".join(gates.reasons)
        return Decision(
            candidate_id=candidate.candidate_id,
            strategy=self.name,
            decision=decision,
            reason=reason,
            accepted=gates.ok,
            decision_at_ms=now_ms(),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            confidence=None,
            input_state=candidate.jev_state(),
            output={"gate_reasons": gates.reasons, "gate_type": "deterministic"},
        )


class JevAssistedStrategy:
    name = "jev"

    def __init__(self, config: AppConfig, provider: JevProvider):
        self.config = config
        self.provider = provider

    async def evaluate(self, candidate: Opportunity, portfolio: InventoryBook) -> Decision:
        started = time.perf_counter()
        gates = common_safety_gate(candidate, portfolio, self.config)
        output: dict[str, Any]
        try:
            output = await asyncio.wait_for(self.provider.decide(candidate), timeout=self.config.jev_timeout_ms / 1000.0)
        except (asyncio.TimeoutError, JevUnavailable, ValueError, OSError) as exc:
            latency = (time.perf_counter() - started) * 1000.0
            return Decision(
                candidate_id=candidate.candidate_id,
                strategy=self.name,
                decision="SKIP",
                reason=f"jev_fail_closed:{type(exc).__name__}:{exc}",
                accepted=False,
                decision_at_ms=now_ms(),
                latency_ms=latency,
                confidence=None,
                input_state=candidate.jev_state(),
                output={"error": str(exc), "error_type": type(exc).__name__, "hard_gate_reasons": gates.reasons},
            )
        choice = output.get("execution_decision")
        probabilities = output.get("execution_decision_probabilities") or {}
        confidence = output.get("execution_decision_confidence")
        risk = output.get("execution_risk")
        jev_reasons: list[str] = []
        if gates.reasons:
            jev_reasons.extend(f"hard_safety:{reason}" for reason in gates.reasons)
        if choice != "EXECUTE":
            jev_reasons.append("jev_decided_skip")
        if risk == "HIGH":
            jev_reasons.append("jev_execution_risk_high")
        if float(probabilities.get("EXECUTE", 0.0)) < self.config.jev_min_execute_probability:
            jev_reasons.append("jev_execute_probability_below_threshold")
        if confidence is not None and float(confidence) < self.config.jev_min_confidence:
            jev_reasons.append("jev_confidence_below_threshold")
        accepted = not jev_reasons
        return Decision(
            candidate_id=candidate.candidate_id,
            strategy=self.name,
            decision="EXECUTE" if accepted else "SKIP",
            reason="jev_and_safety_gates_passed" if accepted else ";".join(jev_reasons),
            accepted=accepted,
            decision_at_ms=now_ms(),
            latency_ms=(time.perf_counter() - started) * 1000.0,
            confidence=float(confidence) if confidence is not None else None,
            input_state=candidate.jev_state(),
            output={**output, "hard_gate_reasons": gates.reasons},
        )


