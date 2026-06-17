from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_quant_trader.core.models import (
    Alignment,
    MarketBackgroundSnapshot,
    NewsDigest,
    NewsDirection,
    NewsEvent,
    NewsItem,
    NewsSeverity,
)
from ai_quant_trader.storage.sqlite import SQLiteStore


BEARISH_KEYWORDS = (
    "rate hike",
    "higher rates",
    "hawkish",
    "inflation hot",
    "inflation above",
    "cpi above",
    "pce above",
    "yields rise",
    "dollar strength",
    "dollar index rises",
    "tariff",
    "sanction",
    "war escalates",
    "lawsuit",
    "sec sues",
    "crackdown",
    "hack",
    "exploit",
    "withdrawal halted",
    "bankruptcy",
    "\u52a0\u606f",
    "\u9e70\u6d3e",
    "\u901a\u80c0\u8d85\u9884\u671f",
    "\u7f8e\u5143\u8d70\u5f3a",
    "\u6536\u76ca\u7387\u4e0a\u5347",
    "\u5173\u7a0e",
    "\u5236\u88c1",
    "\u6218\u4e89\u5347\u7ea7",
    "\u8d77\u8bc9",
    "\u76d1\u7ba1\u6253\u51fb",
    "\u9ed1\u5ba2",
    "\u653b\u51fb",
    "\u6682\u505c\u63d0\u73b0",
)

BULLISH_KEYWORDS = (
    "rate cut",
    "cuts rates",
    "dovish",
    "inflation cools",
    "cpi below",
    "pce below",
    "yields fall",
    "dollar weak",
    "stimulus",
    "liquidity injection",
    "etf approval",
    "etf inflow",
    "spot etf inflow",
    "regulatory clarity",
    "ceasefire",
    "\u964d\u606f",
    "\u9e3d\u6d3e",
    "\u901a\u80c0\u964d\u6e29",
    "\u4f4e\u4e8e\u9884\u671f",
    "\u7f8e\u5143\u8d70\u5f31",
    "\u6536\u76ca\u7387\u4e0b\u884c",
    "\u523a\u6fc0\u8ba1\u5212",
    "\u6d41\u52a8\u6027\u6295\u653e",
    "etf\u6279\u51c6",
    "etf\u6d41\u5165",
    "\u505c\u706b",
)

CRITICAL_KEYWORDS = (
    "emergency rate",
    "exchange hacked",
    "hack",
    "exploit",
    "withdrawal halted",
    "bankruptcy",
    "default",
    "war attack",
    "invasion",
    "missile",
    "\u7d27\u6025\u964d\u606f",
    "\u4ea4\u6613\u6240\u88ab\u9ed1",
    "\u9ed1\u5ba2",
    "\u653b\u51fb",
    "\u6682\u505c\u63d0\u73b0",
    "\u7834\u4ea7",
    "\u8fdd\u7ea6",
    "\u5bfc\u5f39",
)

HIGH_IMPACT_KEYWORDS = (
    "fomc",
    "fed",
    "powell",
    "cpi",
    "pce",
    "nonfarm",
    "payrolls",
    "unemployment",
    "interest rate",
    "rate decision",
    "dollar index",
    "treasury yield",
    "oil",
    "sec",
    "etf",
    "tariff",
    "sanction",
    "geopolitical",
    "\u7f8e\u8054\u50a8",
    "\u9c8d\u5a01\u5c14",
    "\u975e\u519c",
    "\u5931\u4e1a\u7387",
    "\u5229\u7387\u51b3\u8bae",
    "\u7f8e\u5143\u6307\u6570",
    "\u7f8e\u503a\u6536\u76ca\u7387",
    "\u539f\u6cb9",
    "\u8bc1\u76d1\u4f1a",
    "\u5173\u7a0e",
    "\u5730\u7f18",
)

SEVERITY_WEIGHT = {
    NewsSeverity.LOW: 0.25,
    NewsSeverity.MEDIUM: 0.55,
    NewsSeverity.HIGH: 0.9,
    NewsSeverity.CRITICAL: 1.25,
}


class MarketNewsContextBuilder:
    """Builds deterministic market-impact background for AI prompts.

    The module does not invent facts and does not call DeepSeek. It preserves
    raw news items, labels direction/risk with deterministic rules, keeps
    event-level decay windows, and emits a compact snapshot for the trading AI.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        lookback_hours: int = 48,
        realtime_minutes: int = 60,
        max_events: int = 80,
    ) -> None:
        self.store = store
        self.lookback_hours = lookback_hours
        self.realtime_minutes = realtime_minutes
        self.max_events = max_events

    def update_digest(self, digest: NewsDigest, now: datetime | None = None) -> NewsDigest:
        now_utc = self._now(now)
        current_events = [self._event_from_item(item, now_utc) for item in digest.items]
        self._persist_new_events(current_events)
        return self._attach_snapshot(digest, current_events, now_utc, persist_snapshot=True)

    def attach_latest_background(self, digest: NewsDigest, now: datetime | None = None) -> NewsDigest:
        now_utc = self._now(now)
        current_events = [self._event_from_item(item, now_utc) for item in digest.items]
        self._persist_new_events(current_events)
        return self._attach_snapshot(digest, current_events, now_utc, persist_snapshot=False)

    def _attach_snapshot(
        self,
        digest: NewsDigest,
        current_events: list[NewsEvent],
        now_utc: datetime,
        *,
        persist_snapshot: bool,
    ) -> NewsDigest:
        realtime_cutoff = now_utc - timedelta(minutes=self.realtime_minutes)
        realtime_events = [
            event for event in current_events
            if event.published_at.astimezone(UTC) >= realtime_cutoff and event.severity != NewsSeverity.LOW
        ]
        active_by_id: dict[str, NewsEvent] = {}
        for event in self._load_recent_events():
            if event.decay_until.astimezone(UTC) >= now_utc:
                active_by_id[event.event_id] = event
        for event in current_events:
            if event.decay_until.astimezone(UTC) >= now_utc:
                active_by_id[event.event_id] = event
        active_events = sorted(
            active_by_id.values(),
            key=lambda event: (
                SEVERITY_WEIGHT.get(event.severity, 0.0),
                event.risk_score,
                event.published_at,
            ),
            reverse=True,
        )[: self.max_events]
        snapshot = self._snapshot(active_events, realtime_events, now_utc)
        if persist_snapshot:
            self.store.insert("market_background_snapshots", snapshot.model_dump(mode="json"))
        summary = self._combine_summary(snapshot.summary, digest.summary)
        warnings = list(dict.fromkeys([*digest.warnings, *snapshot.warnings, "market_background_attached"]))
        macro_risk_level = "high" if snapshot.risk_level == "critical" else snapshot.risk_level
        if macro_risk_level not in {"low", "medium", "high"}:
            macro_risk_level = digest.macro_risk_level
        crypto_sentiment = {
            NewsDirection.BULLISH: Alignment.ALIGNED,
            NewsDirection.BEARISH: Alignment.CONFLICT,
            NewsDirection.NEUTRAL: Alignment.NEUTRAL,
            NewsDirection.UNKNOWN: Alignment.UNKNOWN,
        }[snapshot.background_direction]
        return digest.model_copy(
            update={
                "summary": summary,
                "warnings": warnings,
                "news_direction": snapshot.background_direction,
                "crypto_sentiment": crypto_sentiment,
                "macro_risk_level": macro_risk_level,
                "active_news_events": active_events[:20],
                "market_background": snapshot,
            }
        )

    def _snapshot(
        self,
        active_events: list[NewsEvent],
        realtime_events: list[NewsEvent],
        now_utc: datetime,
    ) -> MarketBackgroundSnapshot:
        direction = self._weighted_direction(active_events)
        risk_level = self._risk_level(active_events)
        summary = self._summary(active_events, realtime_events, direction, risk_level)
        warnings = []
        if active_events:
            warnings.append("market_background_uses_decayed_events")
        if realtime_events:
            warnings.append("realtime_news_window_attached")
        return MarketBackgroundSnapshot(
            generated_at=now_utc,
            lookback_hours=self.lookback_hours,
            realtime_minutes=self.realtime_minutes,
            background_direction=direction,
            risk_level=risk_level,
            active_events=active_events[:20],
            realtime_events=realtime_events[:12],
            summary=summary,
            warnings=warnings,
        )

    def _persist_new_events(self, events: list[NewsEvent]) -> None:
        if not events:
            return
        existing = {
            str((row.get("payload") or {}).get("event_id") or "")
            for row in self.store.fetch_payloads("news_events", limit=1000)
        }
        for event in events:
            if event.event_id in existing:
                continue
            self.store.insert("news_events", event.model_dump(mode="json"), symbol=event.event_id)
            existing.add(event.event_id)

    def _load_recent_events(self) -> list[NewsEvent]:
        output: list[NewsEvent] = []
        for row in self.store.fetch_payloads("news_events", limit=1000):
            payload = row.get("payload") or {}
            try:
                output.append(NewsEvent.model_validate(payload))
            except Exception:
                continue
        return output

    def _event_from_item(self, item: NewsItem, now_utc: datetime) -> NewsEvent:
        text = self._text(item)
        bearish_hits = self._hits(text, BEARISH_KEYWORDS)
        bullish_hits = self._hits(text, BULLISH_KEYWORDS)
        critical_hits = self._hits(text, CRITICAL_KEYWORDS)
        high_hits = self._hits(text, HIGH_IMPACT_KEYWORDS)
        if bearish_hits > bullish_hits:
            direction = NewsDirection.BEARISH
        elif bullish_hits > bearish_hits:
            direction = NewsDirection.BULLISH
        elif bullish_hits or bearish_hits:
            direction = NewsDirection.NEUTRAL
        else:
            direction = NewsDirection.UNKNOWN
        severity = self._severity(item, critical_hits, high_hits, bullish_hits + bearish_hits)
        risk_score = self._risk_score(item, severity, critical_hits, high_hits, bullish_hits + bearish_hits)
        confidence = self._confidence(item, critical_hits + high_hits + bullish_hits + bearish_hits)
        published_at = item.published_at.astimezone(UTC)
        decay_until = published_at + timedelta(hours=self._decay_hours(severity))
        if decay_until < now_utc and severity in {NewsSeverity.HIGH, NewsSeverity.CRITICAL}:
            decay_until = now_utc + timedelta(minutes=30)
        key = self._source_key(item)
        return NewsEvent(
            event_id=self._event_id(key),
            title=item.title,
            source=item.source,
            published_at=published_at,
            category=item.category or "macro",
            direction=direction,
            severity=severity,
            risk_score=risk_score,
            confidence=confidence,
            asset_scope=self._asset_scope(item, text),
            summary=item.summary or item.title,
            decay_until=decay_until,
            source_item_key=key,
        )

    def _severity(self, item: NewsItem, critical_hits: int, high_hits: int, directional_hits: int) -> NewsSeverity:
        category = (item.category or "").lower()
        if critical_hits:
            return NewsSeverity.CRITICAL
        if high_hits or category in {"macro", "politics", "geopolitical", "regulation"}:
            return NewsSeverity.HIGH
        if directional_hits or item.credibility >= 0.8:
            return NewsSeverity.MEDIUM
        return NewsSeverity.LOW

    def _risk_score(
        self,
        item: NewsItem,
        severity: NewsSeverity,
        critical_hits: int,
        high_hits: int,
        directional_hits: int,
    ) -> float:
        base = {
            NewsSeverity.LOW: 0.15,
            NewsSeverity.MEDIUM: 0.4,
            NewsSeverity.HIGH: 0.68,
            NewsSeverity.CRITICAL: 0.9,
        }[severity]
        score = base + min(0.2, item.credibility * 0.12) + min(0.16, (critical_hits + high_hits + directional_hits) * 0.04)
        return min(1.0, round(score, 4))

    def _confidence(self, item: NewsItem, hit_count: int) -> float:
        score = 0.25 + min(0.45, item.credibility * 0.45) + min(0.3, hit_count * 0.08)
        return min(1.0, round(score, 4))

    def _weighted_direction(self, events: list[NewsEvent]) -> NewsDirection:
        if not events:
            return NewsDirection.UNKNOWN
        score = 0.0
        for event in events:
            weight = SEVERITY_WEIGHT.get(event.severity, 0.0) * max(event.risk_score, 0.1) * max(event.confidence, 0.1)
            if event.direction == NewsDirection.BULLISH:
                score += weight
            elif event.direction == NewsDirection.BEARISH:
                score -= weight
        if abs(score) < 0.12:
            return NewsDirection.NEUTRAL
        return NewsDirection.BULLISH if score > 0 else NewsDirection.BEARISH

    def _risk_level(self, events: list[NewsEvent]) -> str:
        if not events:
            return "unknown"
        severity_rank = {
            NewsSeverity.LOW: 1,
            NewsSeverity.MEDIUM: 2,
            NewsSeverity.HIGH: 3,
            NewsSeverity.CRITICAL: 4,
        }
        highest = max(events, key=lambda event: severity_rank.get(event.severity, 0)).severity
        return highest.value

    def _summary(
        self,
        active_events: list[NewsEvent],
        realtime_events: list[NewsEvent],
        direction: NewsDirection,
        risk_level: str,
    ) -> str:
        lines = [
            f"Market background: direction={direction.value}, risk={risk_level}, active_events={len(active_events)}, realtime_events={len(realtime_events)}."
        ]
        for event in active_events[:8]:
            when = event.published_at.strftime("%m-%d %H:%M")
            lines.append(
                f"- {when} [{event.source}] {event.severity.value}/{event.direction.value}: {event.title[:180]}"
            )
        if realtime_events:
            lines.append("Realtime window:")
            for event in realtime_events[:6]:
                when = event.published_at.strftime("%H:%M")
                lines.append(f"- {when} [{event.source}] {event.severity.value}/{event.direction.value}: {event.title[:160]}")
        return "\n".join(lines)

    def _combine_summary(self, background: str, current: str) -> str:
        if not current:
            return background
        if background in current:
            return current
        return f"{background}\n\nRealtime digest:\n{current}"

    def _asset_scope(self, item: NewsItem, text: str) -> list[str]:
        scope = {"risk_assets"}
        category = (item.category or "").lower()
        if category in {"crypto", "regulation"} or any(token in text for token in ("crypto", "bitcoin", "ethereum", "eth", "btc")):
            scope.update({"crypto", "ETH", "BTC"})
        if category in {"macro", "politics", "geopolitical"}:
            scope.update({"macro", "USD", "rates"})
        return sorted(scope)

    def _decay_hours(self, severity: NewsSeverity) -> int:
        return {
            NewsSeverity.CRITICAL: 72,
            NewsSeverity.HIGH: 48,
            NewsSeverity.MEDIUM: 24,
            NewsSeverity.LOW: 8,
        }[severity]

    def _source_key(self, item: NewsItem) -> str:
        raw = item.url or f"{item.source}:{item.title}"
        normalized = re.sub(r"\s+", " ", raw.strip().lower())
        return normalized[:400]

    def _event_id(self, key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    def _text(self, item: NewsItem) -> str:
        return f"{item.title} {item.summary} {item.category} {item.source}".lower()

    def _hits(self, text: str, keywords: tuple[str, ...]) -> int:
        return sum(1 for keyword in keywords if keyword in text)

    def _now(self, now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(UTC)
        if now.tzinfo is None:
            return now.replace(tzinfo=UTC)
        return now.astimezone(UTC)
