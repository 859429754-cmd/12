from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from ai_quant_trader.core.models import (
    AiDecision,
    AggregatedOrderflow,
    DenseZone,
    NewsDigest,
    NewsItem,
    RiskDecision,
    StrategySignal,
)


class HourlyReportBuilder:
    """构建手机端容易扫读的中文小时报告。"""

    def build(
        self,
        rows: list[tuple[StrategySignal, AiDecision, AggregatedOrderflow, DenseZone, RiskDecision]],
        news: NewsDigest | None = None,
        major_news_only: bool = False,
    ) -> str:
        lines = ["# AI盘面汇总", ""]
        for signal, ai, orderflow, dense_zone, risk in rows:
            lines.extend(self._symbol_card(signal, ai, orderflow, dense_zone, risk))
            lines.append("")
        lines.append("> 开仓必须先有技术信号，再经AI、消息面、订单流、授权和硬风控确认。")
        return "\n".join(lines)

    def news_report(self, news: NewsDigest, major_only: bool = False) -> str | None:
        items = self._fresh_report_news(news.items, major_only=major_only)
        if not items:
            return None
        lines = ["# 消息面快讯", ""]
        for item in items[:8]:
            lines.extend(self._news_card(item))
            lines.append("")
        lines.append("> 只展示最近1-3小时新消息；7天长期影响消息进入AI记忆，不重复刷屏。")
        return "\n".join(lines)

    def news_brief(self, news: NewsDigest, major_only: bool = False) -> list[str]:
        items = self._fresh_report_news(news.items, major_only=major_only)
        lines: list[str] = []
        for item in items[:8]:
            lines.extend(self._news_card(item))
            lines.append("")
        return lines

    def _news_card(self, item: NewsItem) -> list[str]:
        bias, level = self._news_bias(item)
        marker = "【重要】" if level == "重要" else "【关注】"
        title = self._short(self._chinese_news_title(item), 72)
        detail = self._short(item.summary or item.title, 110)
        impact = self._short(self._news_explanation(item, bias), 92)
        return [
            f"**{self._format_time(item)}  {marker}{bias}**",
            f"{title}",
            f"细节：{detail}",
            f"> {impact}",
        ]

    def _fresh_report_news(self, items: list[NewsItem], major_only: bool = False) -> list[NewsItem]:
        ranked = self._rank_news(items)
        if major_only:
            ranked = [item for item in ranked if self._news_bias(item)[1] == "重要"]
        now = datetime.now(UTC)
        fresh_90m = [item for item in ranked if now - item.published_at.astimezone(UTC) <= timedelta(minutes=90)]
        if len(fresh_90m) >= 3:
            return fresh_90m
        return [item for item in ranked if now - item.published_at.astimezone(UTC) <= timedelta(hours=3)]

    def trade_alert(self, title: str, signal: StrategySignal, ai: AiDecision, risk: RiskDecision, price: float) -> str:
        action = self._action(signal.action)
        return "\n".join(
            [
                f"# {self._title(title)}",
                f"**{self._symbol(signal.symbol)}｜{action}｜数量 {risk.clipped_qty:.6g}**",
                f"价格 {self._price(price)}｜AI置信度 {ai.confidence:.0%}｜风控余量 {self._money(risk.remaining_notional)}",
                f"估算止盈 {self._fmt(ai.tp_estimate)}｜估算止损 {self._fmt(ai.sl_estimate)}",
                f"原因：{self._short(self._translate_reason(ai.brief_reason or risk.reason), 90)}",
            ]
        )

    def close_alert(self, title: str, orders: list, reason: str) -> str:
        lines = [f"# {title}"]
        if not orders:
            lines.append("当前未检测到需要平仓的 Gate 持仓。")
            return "\n".join(lines)
        for order in orders:
            side = "买入平空" if order.side == "buy" else "卖出平多"
            lines.append(f"- {self._symbol(order.symbol)}：{side}，数量 {order.amount:.8g}，状态 {order.status}")
        lines.append(f"触发原因：{reason}")
        return "\n".join(lines)

    def render_image_card(
        self,
        rows: list[tuple[StrategySignal, AiDecision, AggregatedOrderflow, DenseZone, RiskDecision]],
        news: NewsDigest | None,
        output_path: str,
        major_news_only: bool = False,
    ) -> str | None:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception:  # pragma: no cover
            return None

        news_items = self._fresh_report_news(news.items if news else [], major_only=major_news_only)
        width = 1120
        card_h = 230
        news_h = 58 + min(len(news_items), 5) * 86 if news_items else 70
        height = 120 + max(1, len(rows)) * card_h + news_h
        image = Image.new("RGB", (width, height), "#f6f7fb")
        draw = ImageDraw.Draw(image)

        font_regular = self._font(ImageFont, 32)
        font_small = self._font(ImageFont, 24)
        font_tiny = self._font(ImageFont, 21)
        font_title = self._font(ImageFont, 42)
        draw.text((36, 30), "AI盘面汇总", fill="#111827", font=font_title)
        y = 100
        colors = ["#eef6ff", "#fff7ed", "#f0fdf4"]
        for index, (signal, ai, orderflow, dense_zone, risk) in enumerate(rows):
            x = 28
            draw.rounded_rectangle((x, y, width - 28, y + card_h - 18), radius=18, fill=colors[index % len(colors)], outline="#d8dee9", width=2)
            title = f"{self._symbol(signal.symbol)}  {self._regime(str(ai.regime))} / {self._direction(str(ai.direction))}  AI {ai.confidence:.0%}"
            status = "允许交易" if risk.allowed else "观望"
            draw.text((x + 26, y + 22), title, fill="#111827", font=font_regular)
            draw.text((width - 180, y + 24), status, fill="#b91c1c" if risk.allowed else "#374151", font=font_small)
            draw.text((x + 26, y + 78), f"现价 {self._price(signal.current_price)}    密集区 {self._fmt(dense_zone.val)} - {self._fmt(dense_zone.vah)}", fill="#1f2937", font=font_small)
            draw.text((x + 26, y + 118), f"订单流 {self._alignment(str(ai.orderflow_alignment))}    买卖比 {orderflow.active_buy_sell_ratio:.2f}    盘口 {orderflow.bid_ask_imbalance:+.2f}", fill="#1f2937", font=font_small)
            draw.text((x + 26, y + 158), f"入场 {self._fmt(ai.entry_zone_estimate)}    止盈 {self._fmt(ai.tp_estimate)}    止损 {self._fmt(ai.sl_estimate)}", fill="#1f2937", font=font_small)
            y += card_h

        if news_items:
            draw.text((36, y), "最近消息面快讯", fill="#111827", font=font_regular)
            y += 50
            for item in news_items[:5]:
                bias, level = self._news_bias(item)
                color = "#b91c1c" if bias == "利空" else "#047857" if bias == "利多" else "#374151"
                draw.rounded_rectangle((36, y, width - 36, y + 72), radius=10, fill="#ffffff", outline="#e5e7eb", width=1)
                draw.text((54, y + 12), f"{self._format_time(item)}  {level}  {bias}", fill=color, font=font_tiny)
                draw.text((260, y + 12), self._short(self._chinese_news_title(item), 45), fill="#111827", font=font_tiny)
                draw.text((54, y + 42), self._short(self._news_explanation(item, bias), 68), fill="#4b5563", font=font_tiny)
                y += 86

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return str(path)

    def _font(self, image_font, size: int):
        for name in ("msyh.ttc", "simhei.ttf", "Arial.ttf"):
            try:
                return image_font.truetype(name, size=size)
            except Exception:
                continue
        return image_font.load_default()

    def _symbol_card(self, signal: StrategySignal, ai: AiDecision, orderflow: AggregatedOrderflow, dense_zone: DenseZone, risk: RiskDecision) -> list[str]:
        status = "允许交易" if risk.allowed else "观望"
        zone = f"{self._fmt(dense_zone.val)} - {self._fmt(dense_zone.vah)}"
        flow = f"{self._alignment(str(ai.orderflow_alignment))}，买卖比 {orderflow.active_buy_sell_ratio:.2f}，盘口 {orderflow.bid_ask_imbalance:+.2f}"
        reason = self._short(self._translate_reason(ai.brief_reason or risk.reason), 96)
        return [
            f"## {self._symbol(signal.symbol)}｜{self._regime(str(ai.regime))}｜{self._direction(str(ai.direction))}｜AI {ai.confidence:.0%}｜{status}",
            f"现价：{self._price(signal.current_price)}｜关键区：{zone}",
            f"订单流：{flow}",
            f"参考点位：入场 {self._fmt(ai.entry_zone_estimate)}｜止盈 {self._fmt(ai.tp_estimate)}｜止损 {self._fmt(ai.sl_estimate)}",
            f"结论：{reason}",
        ]

    def _rank_news(self, items: list[NewsItem]) -> list[NewsItem]:
        keywords = (
            "fed", "fomc", "powell", "rate", "cut", "hike", "inflation", "cpi", "ppi", "jobs",
            "payroll", "employment", "white house", "president", "war", "conflict", "sanction",
            "dollar", "bitcoin", "ethereum", "etf", "stablecoin", "treasury", "bea", "gdp",
            "pce", "tariff", "sec", "recession", "liquidity", "美联储", "特朗普", "伊朗", "参议院",
        )

        def score(item: NewsItem) -> tuple[int, float, datetime]:
            text = f"{item.title} {item.summary} {item.category}".lower()
            return sum(1 for keyword in keywords if keyword in text), item.credibility, item.published_at

        ranked = sorted(items, key=score, reverse=True)
        return [item for item in ranked if score(item)[0] > 0 or item.credibility >= 0.65]

    def _news_bias(self, item: NewsItem) -> tuple[str, str]:
        text = f"{item.title or ''} {item.summary or ''} {item.category or ''}".lower()
        bearish = ("hike", "higher rate", "hot cpi", "inflation", "war", "conflict", "sanction", "lawsuit", "hack", "outflow", "selloff", "ban", "tariff", "hawkish", "加息", "制裁", "战争")
        bullish = ("cut", "rate cut", "dovish", "etf approval", "inflow", "adoption", "reserve", "buy", "stimulus", "approval", "降息", "批准")
        bear_score = sum(1 for word in bearish if word in text)
        bull_score = sum(1 for word in bullish if word in text)
        if bear_score > bull_score:
            bias = "利空"
        elif bull_score > bear_score:
            bias = "利多"
        else:
            bias = "中性"
        level = "重要" if max(bear_score, bull_score) >= 1 or item.credibility >= 0.75 else "关注"
        return bias, level

    def _chinese_news_title(self, item: NewsItem) -> str:
        raw = " ".join((item.title or item.summary or "").split())
        lowered = raw.lower()
        if not raw:
            return "未提供标题"
        if "fed" in lowered or "federal reserve" in lowered or "fomc" in lowered or "美联储" in raw:
            if "cut" in lowered or "降息" in raw:
                return "美联储相关消息显示市场重新交易降息预期"
            if "hike" in lowered or "higher" in lowered or "加息" in raw:
                return "美联储相关消息引发高利率或加息压力担忧"
            if "powell" in lowered or "鲍威尔" in raw:
                return "美联储主席讲话可能影响美元与风险资产"
            return "美联储货币政策路径变化值得关注"
        if "cpi" in lowered or "ppi" in lowered or "inflation" in lowered or "通胀" in raw:
            return "通胀数据相关消息可能影响降息预期和风险偏好"
        if "pce" in lowered or "gdp" in lowered or "bea" in lowered:
            return "美国经济数据可能改变市场对增长和利率路径的判断"
        if "jobs" in lowered or "payroll" in lowered or "employment" in lowered or "非农" in raw:
            return "美国就业数据相关消息可能影响美联储政策预期"
        if "war" in lowered or "conflict" in lowered or "sanction" in lowered or "伊朗" in raw or "以色列" in raw:
            return "地缘政治风险消息可能推升避险情绪"
        if "white house" in lowered or "president" in lowered or "特朗普" in raw or "参议院" in raw or "众议院" in raw:
            return raw
        if "dollar" in lowered or "currency" in lowered or "treasury" in lowered or "美元" in raw:
            return "美元、财政部或货币市场消息可能影响加密资产流动性"
        if "bitcoin" in lowered:
            return "比特币相关消息可能带动加密市场整体风险偏好"
        if "ethereum" in lowered or "ether" in lowered:
            return "以太坊相关消息可能影响ETH及生态资产"
        if "crypto" in lowered or "defi" in lowered or "stablecoin" in lowered or "etf" in lowered:
            return "加密市场消息可能影响行业资金情绪"
        return raw

    def _news_explanation(self, item: NewsItem, bias: str) -> str:
        text = f"{item.title or ''} {item.summary or ''}".lower()
        prefix = f"倾向{bias}。"
        if any(word in text for word in ("fed", "fomc", "powell", "rate", "cut", "hike")):
            return prefix + "利率路径会影响美元强弱和风险资产估值，加密市场短线波动可能放大。"
        if any(word in text for word in ("cpi", "ppi", "inflation", "jobs", "payroll", "pce", "gdp")):
            return prefix + "宏观数据会改变降息或加息预期，突破信号需要结合订单流确认。"
        if any(word in text for word in ("war", "conflict", "sanction", "president", "white house", "tariff")) or any(word in item.title for word in ("特朗普", "伊朗", "参议院", "众议院")):
            return prefix + "政治和地缘事件可能触发避险交易，AI会降低追单权重或直接否决。"
        if any(word in text for word in ("bitcoin", "ethereum", "crypto", "etf", "stablecoin")):
            return prefix + "加密行业消息会影响资金流向，需要结合盘口、密集区和成交量确认。"
        return prefix + "该消息纳入本小时消息面观察，主要作为风险情绪参考。"

    def _translate_reason(self, text: str) -> str:
        mapping = {
            "no_entry_signal": "本地技术策略没有给出开仓信号。",
            "cold_start_or_symbol_not_authorized": "冷启动安全锁或该标的未获授权，禁止新开仓。",
            "ai_direction_conflict": "AI方向判断与技术信号冲突，禁止开仓。",
            "ai_not_trend_regime": "AI判断当前不是趋势行情，暂不追单。",
            "ai_veto_block": "AI触发否决，禁止开仓。",
            "ai_confidence_too_low": "AI置信度不足，暂不交易。",
            "news_major_conflict": "消息面与开仓方向冲突，禁止开仓。",
            "orderflow_conflict": "多交易所订单流与开仓方向冲突，禁止开仓。",
            "max_total_leverage_reached": "总仓位已达到4倍杠杆硬上限。",
            "entry_allowed_by_consensus": "技术面、AI、消息面和订单流形成足够同向共识。",
            "same_direction_position_exists": "检测到同方向已有持仓，禁止重复加仓。",
            "exit_signal_allowed_even_when_opening_paused": "技术平仓信号允许执行。",
        }
        translated = mapping.get(text, text or "暂无补充原因。")
        replacements = {"exit_long": "平多", "exit_short": "平空", "hold": "观望", "range": "震荡", "trend": "趋势", "neutral": "中性", "long": "偏多", "short": "偏空"}
        for raw, chinese in replacements.items():
            translated = translated.replace(raw, chinese)
        return translated

    def _format_time(self, item: NewsItem) -> str:
        try:
            return item.published_at.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            return "时间未知"

    def _fmt(self, value: float | None) -> str:
        return "-" if value is None else self._price(value)

    def _price(self, value: float) -> str:
        value = float(value)
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        if abs(value) >= 100:
            return f"{value:,.2f}"
        if abs(value) >= 1:
            return f"{value:,.3f}".rstrip("0").rstrip(".")
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def _money(self, value: float) -> str:
        return f"{float(value):,.0f} USDT"

    def _symbol(self, symbol: str) -> str:
        return symbol.split("/")[0]

    def _short(self, text: str, limit: int) -> str:
        text = str(text).replace("|", "/")
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _title(self, title: str) -> str:
        return {"Entry Alert": "开仓提醒", "Exit Alert": "平仓提醒"}.get(title, title)

    def _regime(self, value: str) -> str:
        return {"trend": "趋势", "range": "震荡", "uncertain": "不明"}.get(value, value)

    def _direction(self, value: str) -> str:
        return {"long": "偏多", "short": "偏空", "flat": "中性"}.get(value, value)

    def _alignment(self, value: str) -> str:
        return {"aligned": "同向", "conflict": "冲突", "neutral": "中性", "unknown": "未知"}.get(value, value)

    def _action(self, value) -> str:
        return {"long": "做多", "short": "做空", "exit_long": "平多", "exit_short": "平空", "hold": "观望"}.get(str(value), str(value))
