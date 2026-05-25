from __future__ import annotations

from statistics import mean
from typing import Any

from ai_quant_trader.core.models import AiDecision, AiDriftReport, HealthStatus, Side
from ai_quant_trader.storage.sqlite import SQLiteStore

SCORE_FIELDS = [
    "trend_confirmation_score",
    "range_risk_score",
    "news_risk_score",
    "orderflow_confirmation_score",
    "dense_zone_breakout_score",
]


class AIDriftMonitor:
    """Detect abrupt DeepSeek output drift against recent decisions."""

    def __init__(self, store: SQLiteStore, *, lookback: int = 20, warn_delta: float = 0.3, block_delta: float = 0.55) -> None:
        self.store = store
        self.lookback = lookback
        self.warn_delta = warn_delta
        self.block_delta = block_delta

    def evaluate(self, symbol: str, decision: AiDecision) -> AiDriftReport:
        baseline = self._recent_decisions(symbol)
        if len(baseline) < 3:
            return AiDriftReport(
                symbol=symbol,
                status=HealthStatus.OK,
                reason="ai_drift_insufficient_history",
                sample_size=len(baseline),
                latest_confidence=decision.confidence,
                latest_direction=decision.direction,
            )
        baseline_confidence = mean(item.confidence for item in baseline)
        baseline_direction = self._dominant_direction(baseline)
        score_deltas = {
            field: abs(float(getattr(decision, field)) - mean(float(getattr(item, field)) for item in baseline))
            for field in SCORE_FIELDS
        }
        confidence_delta = abs(decision.confidence - baseline_confidence)
        direction_flip = (
            baseline_direction is not None
            and decision.direction != Side.FLAT
            and baseline_direction != Side.FLAT
            and decision.direction != baseline_direction
            and decision.confidence >= 0.75
            and baseline_confidence >= 0.7
        )
        drift_score = min(max([confidence_delta, *score_deltas.values()], default=0.0), 1.0)
        status = HealthStatus.OK
        reason = "ai_drift_ok"
        if direction_flip or drift_score >= self.block_delta:
            status = HealthStatus.BLOCK
            reason = "ai_output_drift_block"
        elif drift_score >= self.warn_delta:
            status = HealthStatus.WARN
            reason = "ai_output_drift_warn"
        return AiDriftReport(
            symbol=symbol,
            status=status,
            reason=reason,
            drift_score=drift_score,
            sample_size=len(baseline),
            latest_confidence=decision.confidence,
            baseline_confidence=baseline_confidence,
            latest_direction=decision.direction,
            baseline_direction=baseline_direction,
            score_deltas=score_deltas,
        )

    def _recent_decisions(self, symbol: str) -> list[AiDecision]:
        output: list[AiDecision] = []
        for row in self.store.fetch_payloads("ai_decisions", limit=self.lookback * 2, symbol=symbol):
            payload = row.get("payload") or {}
            normalized = self._extract_decision_payload(payload)
            if normalized is None:
                continue
            try:
                output.append(AiDecision.model_validate(normalized))
            except Exception:  # noqa: BLE001
                continue
            if len(output) >= self.lookback:
                break
        return output

    def _extract_decision_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if "confidence" in payload and "veto_action" in payload:
            return payload
        nested = payload.get("ai")
        if isinstance(nested, dict) and "confidence" in nested and "veto_action" in nested:
            return nested
        return None

    def _dominant_direction(self, decisions: list[AiDecision]) -> Side | None:
        counts = {Side.LONG: 0, Side.SHORT: 0, Side.FLAT: 0}
        for item in decisions:
            counts[item.direction] = counts.get(item.direction, 0) + 1
        return max(counts, key=counts.get) if decisions else None
