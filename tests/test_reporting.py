from __future__ import annotations

from ai_quant_trader.core.models import (
    AggregatedOrderflow,
    AiDecision,
    DenseZone,
    MarketRegime,
    NewsDigest,
    NewsItem,
    RiskDecision,
    Side,
    SignalAction,
    StrategySignal,
)
from ai_quant_trader.reporting.hourly import HourlyReportBuilder


def test_hourly_report_contains_three_chinese_cards() -> None:
    builder = HourlyReportBuilder()
    rows = []
    for symbol in ["ETH/USDT:USDT", "BTC/USDT:USDT", "SOL/USDT:USDT"]:
        rows.append(
            (
                StrategySignal(symbol=symbol, timeframe="1h", action=SignalAction.HOLD, current_price=100),
                AiDecision(symbol=symbol, regime=MarketRegime.RANGE, direction=Side.FLAT, confidence=0.4, multiplier=0.5),
                AggregatedOrderflow(symbol=symbol, depth_usd=1_000_000),
                DenseZone(symbol=symbol, poc=100, vah=110, val=90),
                RiskDecision(allowed=False, action=SignalAction.HOLD, symbol=symbol, reason="no_entry_signal"),
            )
        )
    report = builder.build(rows)
    assert "| 标的 |" not in report
    assert "AI盘面汇总" in report
    assert "ETH" in report
    assert "关键区" in report
    assert "订单流" in report


def test_news_report_uses_mobile_friendly_timeline() -> None:
    builder = HourlyReportBuilder()
    news = NewsDigest(
        items=[
            NewsItem(
                title="Fed rate hike worries return as inflation stays hot",
                source="Federal Reserve",
                category="macro",
                credibility=0.9,
            )
        ]
    )
    report = builder.news_report(news) or ""
    assert "消息面快讯" in report
    assert "| 时间 |" not in report
    assert "【重要】" in report
    assert "利空" in report


def test_major_news_only_filters_low_importance_news() -> None:
    builder = HourlyReportBuilder()
    news = NewsDigest(
        items=[
            NewsItem(title="Minor exchange listing update", source="Crypto Blog", category="crypto", credibility=0.55),
            NewsItem(title="Fed rate hike worries return", source="Federal Reserve", category="macro", credibility=0.9),
        ]
    )
    report = builder.news_report(news, major_only=True) or ""
    assert "美联储" in report or "Fed" in report or "利空" in report
    assert "Minor exchange" not in report
