from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from collections import defaultdict
from pathlib import Path
from typing import Any


def _loads(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _fetch(conn: sqlite3.Connection, table: str, limit: int) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            f"SELECT id, created_at, symbol, payload FROM {table} ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def audit_deepseek_usage(db_path: Path, *, limit: int = 5000) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        budget_rows = _fetch(conn, "ai_call_budget_events", limit)
        usage_rows = _fetch(conn, "ai_call_usage_events", limit)
        news_rows = _fetch(conn, "news_risk_reviews", limit)
        decision_rows = _fetch(conn, "ai_decisions", limit)
    finally:
        conn.close()

    budget_by_type: Counter[str] = Counter()
    budget_by_status: Counter[str] = Counter()
    budget_by_reason: Counter[str] = Counter()
    budget_by_day: Counter[str] = Counter()
    for row in budget_rows:
        payload = _loads(row["payload"])
        budget_by_type[str(payload.get("call_type") or "unknown")] += 1
        budget_by_status[str(payload.get("status") or "unknown")] += 1
        budget_by_reason[str(payload.get("reason") or "")] += 1
        budget_by_day[str(row["created_at"] or "")[:10]] += 1

    usage_by_day: dict[str, dict[str, int]] = defaultdict(lambda: Counter())
    usage_by_type: dict[str, dict[str, int]] = defaultdict(lambda: Counter())
    usage_by_credential: dict[str, dict[str, int]] = defaultdict(lambda: Counter())
    usage_status: Counter[str] = Counter()
    for row in usage_rows:
        payload = _loads(row["payload"])
        day = str(row["created_at"] or "")[:10]
        call_type = str(payload.get("call_type") or "unknown")
        credential = str(payload.get("credential_label") or "unknown")
        status = str(payload.get("status") or "unknown")
        hit = int(payload.get("prompt_cache_hit_tokens") or 0)
        miss = int(payload.get("prompt_cache_miss_tokens") or 0)
        prompt = int(payload.get("prompt_tokens") or 0)
        completion = int(payload.get("completion_tokens") or 0)
        total = int(payload.get("total_tokens") or 0)
        for bucket in (usage_by_day[day], usage_by_type[call_type], usage_by_credential[credential]):
            bucket["calls"] += 1
            bucket["prompt_cache_hit_tokens"] += hit
            bucket["prompt_cache_miss_tokens"] += miss
            bucket["prompt_tokens"] += prompt
            bucket["completion_tokens"] += completion
            bucket["total_tokens"] += total
        usage_status[status] += 1

    def finalize_usage(counter: dict[str, int]) -> dict[str, Any]:
        hit = int(counter.get("prompt_cache_hit_tokens") or 0)
        miss = int(counter.get("prompt_cache_miss_tokens") or 0)
        denom = hit + miss
        out = dict(counter)
        out["cache_hit_ratio"] = round(hit / denom, 4) if denom else None
        out["cache_miss_ratio"] = round(miss / denom, 4) if denom else None
        return out

    news_total = len(news_rows)
    news_skipped = 0
    news_called = 0
    news_without_signal = 0
    original_action: Counter[str] = Counter()
    risk_reason: Counter[str] = Counter()
    for row in news_rows:
        payload = _loads(row["payload"])
        if payload.get("status") == "skipped" or payload.get("deepseek_called") is False:
            news_skipped += 1
        else:
            news_called += 1
        signal = payload.get("signal") if isinstance(payload.get("signal"), dict) else {}
        evidence = signal.get("technical_evidence") if isinstance(signal.get("technical_evidence"), dict) else {}
        action = str(evidence.get("original_strategy_action") or signal.get("action") or "unknown")
        original_action[action] += 1
        risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
        reason = str(risk.get("reason") or "")
        risk_reason[reason] += 1
        if action == "hold" and reason == "major_news_without_strategy_signal":
            news_without_signal += 1

    decision_review_type: Counter[str] = Counter()
    for row in decision_rows:
        payload = _loads(row["payload"])
        decision_review_type[str(payload.get("review_type") or "normal_ai_decision")] += 1

    waste_ratio = round(news_without_signal / news_total, 4) if news_total else 0.0
    return {
        "db_path": str(db_path),
        "limit": limit,
        "budget": {
            "total": len(budget_rows),
            "by_type": dict(budget_by_type.most_common()),
            "by_status": dict(budget_by_status.most_common()),
            "top_reasons": dict(budget_by_reason.most_common(12)),
            "by_day": dict(sorted(budget_by_day.items())),
        },
        "usage": {
            "total_events": len(usage_rows),
            "by_status": dict(usage_status.most_common()),
            "by_day": {day: finalize_usage(values) for day, values in sorted(usage_by_day.items())},
            "by_type": {call_type: finalize_usage(values) for call_type, values in sorted(usage_by_type.items())},
            "by_credential": {label: finalize_usage(values) for label, values in sorted(usage_by_credential.items())},
        },
        "major_news_reviews": {
            "total": news_total,
            "deepseek_called_or_legacy": news_called,
            "local_skipped": news_skipped,
            "no_signal_blocked": news_without_signal,
            "no_signal_blocked_ratio": waste_ratio,
            "original_action": dict(original_action.most_common()),
            "top_risk_reasons": dict(risk_reason.most_common(12)),
        },
        "ai_decisions": {
            "total": len(decision_rows),
            "review_type": dict(decision_review_type.most_common()),
        },
        "recommendation": (
            "major_news_prefilter_active"
            if news_skipped > 0
            else "enable_prefilter_for_major_news_without_signal_or_position"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit DeepSeek usage from local SQLite records without printing prompts or secrets.")
    parser.add_argument("--db", default="data/trader.sqlite3")
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()
    report = audit_deepseek_usage(Path(args.db), limit=max(1, args.limit))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
