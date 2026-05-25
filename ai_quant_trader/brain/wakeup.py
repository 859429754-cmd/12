from __future__ import annotations

from ai_quant_trader.core.models import NewsDigest, WakeupEvent, WakeupSeverity


HIGH_IMPACT_KEYWORDS = (
    "fomc", "fed", "rate cut", "rate hike", "cpi", "pce", "nonfarm", "payroll",
    "treasury", "debt ceiling", "government shutdown", "war", "attack", "sanction",
    "oil", "dxy", "dollar", "yield", "sec", "etf", "stablecoin", "exchange hack",
    "美联储", "降息", "加息", "通胀", "非农", "政府关门", "债务上限", "美债",
    "美元", "原油", "战争", "袭击", "制裁", "监管", "稳定币", "交易所",
)

CRITICAL_KEYWORDS = (
    "black swan", "emergency", "crash", "default", "bankruptcy", "halt",
    "黑天鹅", "突发", "崩盘", "违约", "破产", "暂停提现", "战争升级",
)


class WakeupEngine:
    """实时唤醒引擎骨架。

    Flash 负责快筛是否重要，Pro 负责正式交易影响判断。这里先做确定性触发规则，
    后续再接实时价格流、金十快讯流和 DeepSeek 双层分析。
    """

    def __init__(self, price_move_1m_pct: float = 0.8, price_move_5m_pct: float = 1.8):
        self.price_move_1m_pct = price_move_1m_pct
        self.price_move_5m_pct = price_move_5m_pct

    def events_from_news(self, digest: NewsDigest) -> list[WakeupEvent]:
        events: list[WakeupEvent] = []
        for item in digest.items:
            text = f"{item.title} {item.summary} {item.category}".lower()
            severity = WakeupSeverity.LOW
            if any(keyword in text for keyword in HIGH_IMPACT_KEYWORDS):
                severity = WakeupSeverity.HIGH
            if any(keyword in text for keyword in CRITICAL_KEYWORDS):
                severity = WakeupSeverity.CRITICAL
            if severity in {WakeupSeverity.HIGH, WakeupSeverity.CRITICAL}:
                events.append(
                    WakeupEvent(
                        event_type="news",
                        severity=severity,
                        title=item.title,
                        summary=item.summary,
                        source=item.source,
                        raw=item.model_dump(mode="json"),
                        should_escalate_to_pro=severity == WakeupSeverity.CRITICAL,
                    )
                )
        return events

    def event_from_price_move(
        self,
        symbol: str,
        pct_1m: float,
        pct_5m: float,
        volume_ratio: float = 1.0,
    ) -> WakeupEvent | None:
        abs_1m = abs(pct_1m)
        abs_5m = abs(pct_5m)
        if abs_1m < self.price_move_1m_pct and abs_5m < self.price_move_5m_pct and volume_ratio < 2.0:
            return None
        severity = WakeupSeverity.HIGH if abs_5m >= self.price_move_5m_pct or volume_ratio >= 2.0 else WakeupSeverity.MEDIUM
        return WakeupEvent(
            event_type="price_move",
            severity=severity,
            symbol=symbol,
            title=f"{symbol} 出现实时行情异动",
            summary=f"1分钟涨跌幅 {pct_1m:.2f}%，5分钟涨跌幅 {pct_5m:.2f}%，成交量倍率 {volume_ratio:.2f}",
            raw={"pct_1m": pct_1m, "pct_5m": pct_5m, "volume_ratio": volume_ratio},
            should_escalate_to_pro=severity == WakeupSeverity.HIGH,
        )

