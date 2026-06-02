from __future__ import annotations

from pathlib import Path

from ai_quant_trader.storage.sqlite import SQLiteStore
from scripts.deepseek_usage_audit import audit_deepseek_usage


def test_deepseek_usage_audit_counts_skipped_major_news_reviews(tmp_path: Path) -> None:
    store = SQLiteStore(str(tmp_path / "trader.sqlite3"), str(tmp_path / "audit.jsonl"))
    try:
        store.insert(
            "ai_call_budget_events",
            {
                "symbol": "ETH/USDT:USDT",
                "call_type": "major_news_risk_review",
                "status": "skipped",
                "reason": "no_signal_no_position",
            },
            "ETH/USDT:USDT",
        )
        store.insert(
            "news_risk_reviews",
            {
                "review_type": "major_news_risk_review",
                "status": "skipped",
                "deepseek_called": False,
                "signal": {"action": "hold", "technical_evidence": {"original_strategy_action": "hold"}},
                "risk": {"allowed": False, "reason": "major_news_without_strategy_signal"},
            },
            "ETH/USDT:USDT",
        )
    finally:
        store.close()

    report = audit_deepseek_usage(tmp_path / "trader.sqlite3")

    assert report["budget"]["by_status"]["skipped"] == 1
    assert report["major_news_reviews"]["local_skipped"] == 1
    assert report["major_news_reviews"]["no_signal_blocked"] == 1
    assert report["recommendation"] == "major_news_prefilter_active"
