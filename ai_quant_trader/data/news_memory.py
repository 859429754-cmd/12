from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ai_quant_trader.core.models import NewsDigest, NewsItem


LONG_IMPACT_KEYWORDS = (
    "fed", "fomc", "rate", "cut", "hike", "inflation", "cpi", "ppi", "pce",
    "nonfarm", "payroll", "gdp", "recession", "dollar", "dxy", "treasury",
    "yield", "oil", "gold", "debt ceiling", "government shutdown", "tariff",
    "sanction", "war", "conflict", "ceasefire", "sec", "etf", "stablecoin",
    "regulation", "美联储", "降息", "加息", "通胀", "非农", "美元", "美债",
    "收益率", "原油", "黄金", "债务上限", "政府关门", "制裁", "战争", "冲突",
    "监管", "稳定币",
)

SHORT_NOISE_KEYWORDS = (
    "wrap", "preview", "recap", "technical analysis", "price analysis", "opinion",
    "周评", "日评", "技术分析", "行情分析", "市场综述",
)


class NewsMemoryStore:
    """消息面记忆文件。

    - 最近7天的重要长期影响消息会写入 AI 上下文。
    - 超过30天的消息自动清理。
    - 短期噪音、普通复盘、普通行情评论不进入长期记忆。
    """

    def __init__(self, path: str = "data/news_memory.json", context_days: int = 7, retention_days: int = 30):
        self.path = Path(path)
        self.context_days = context_days
        self.retention_days = retention_days

    def update(self, digest: NewsDigest) -> NewsDigest:
        records = self._load()
        existing = {record["key"] for record in records}
        for item in digest.items:
            if not self._is_long_impact(item):
                continue
            key = self._key(item)
            if key in existing:
                continue
            records.append(
                {
                    "key": key,
                    "title": item.title,
                    "summary": item.summary or item.title,
                    "source": item.source,
                    "category": item.category,
                    "published_at": item.published_at.astimezone(UTC).isoformat(),
                    "credibility": item.credibility,
                    "importance": self._importance_score(item),
                }
            )
            existing.add(key)
        records = self._cleanup(records)
        self._save(records)
        self.write_context_file(records=records, days=2)
        return self.enrich_digest(digest, records)

    def enrich_digest(self, digest: NewsDigest, records: list[dict[str, Any]] | None = None) -> NewsDigest:
        records = self._load() if records is None else records
        context = self._context_records(records)
        if not context:
            return digest
        lines = []
        for record in context[:12]:
            when = self._parse_dt(record["published_at"]).strftime("%m-%d")
            title = str(record.get("title") or "")[:120]
            lines.append(f"{when} {title}")
        summary = "最近7天长期影响消息记忆：" + "；".join(lines)
        if digest.summary:
            summary = summary + "。本小时摘要：" + digest.summary
        return digest.model_copy(update={"summary": summary})

    def context_summary(self, days: int = 2, limit: int = 20) -> str:
        records = self._context_records_for_days(self._load(), days)
        if not records:
            return ""
        lines = []
        for record in records[:limit]:
            when = self._parse_dt(record["published_at"]).strftime("%m-%d %H:%M")
            title = str(record.get("title") or "")[:140]
            summary = str(record.get("summary") or "")[:220]
            source = str(record.get("source") or "")
            lines.append(f"{when} [{source}] {title} - {summary}")
        return f"最近{days}天重点新闻上下文：" + "\n".join(lines)

    def write_context_file(
        self,
        output_path: str | None = None,
        *,
        records: list[dict[str, Any]] | None = None,
        days: int = 2,
        limit: int = 30,
    ) -> Path:
        path = Path(output_path) if output_path else self.path.with_name("news_context_48h.md")
        records = self._context_records_for_days(self._load() if records is None else records, days)
        lines = [
            f"# 最近{days}天重点新闻上下文",
            "",
            "用途：交易周期和重大新闻复评交给 AI 的本地消息面背景。只记录公开新闻事实，不包含密钥。",
            "",
        ]
        if not records:
            lines.append("暂无可用重点新闻。")
        for record in records[:limit]:
            when = self._parse_dt(record["published_at"]).strftime("%Y-%m-%d %H:%M UTC")
            title = str(record.get("title") or "").strip()
            summary = str(record.get("summary") or "").strip()
            source = str(record.get("source") or "").strip()
            category = str(record.get("category") or "").strip()
            lines.append(f"- {when} [{source}/{category}] {title}；{summary}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return path

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        records.sort(key=lambda row: (row.get("importance", 0), row.get("published_at", "")), reverse=True)
        self.path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    def _cleanup(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        output = []
        seen: set[str] = set()
        for record in records:
            key = str(record.get("key") or "")
            if not key or key in seen:
                continue
            if self._parse_dt(str(record.get("published_at") or "")).astimezone(UTC) < cutoff:
                continue
            seen.add(key)
            output.append(record)
        return output

    def _context_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._context_records_for_days(records, self.context_days)

    def _context_records_for_days(self, records: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        output = [record for record in records if self._parse_dt(str(record.get("published_at") or "")).astimezone(UTC) >= cutoff]
        output.sort(key=lambda row: (row.get("importance", 0), row.get("published_at", "")), reverse=True)
        return output

    def _is_long_impact(self, item: NewsItem) -> bool:
        text = f"{item.title} {item.summary} {item.category}".lower()
        if any(word in text for word in SHORT_NOISE_KEYWORDS):
            return False
        return self._importance_score(item) >= 2 or item.credibility >= 0.9

    def _importance_score(self, item: NewsItem) -> int:
        text = f"{item.title} {item.summary} {item.category}".lower()
        return sum(1 for keyword in LONG_IMPACT_KEYWORDS if keyword in text)

    def _key(self, item: NewsItem) -> str:
        text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", item.title.lower())
        return text[:140]

    def _parse_dt(self, value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            return datetime.now(UTC)


class DailyNewsFlashStore:
    """Stores today's important news flashes for AI review at signal time."""

    def __init__(self, root: str = "data/news_daily", timezone_name: str = "Asia/Shanghai"):
        self.root = Path(root)
        self.timezone = ZoneInfo(timezone_name)

    def update(self, digest: NewsDigest, now: datetime | None = None) -> NewsDigest:
        records = self._load_today(now)
        existing = {str(record.get("key") or "") for record in records}
        changed = False
        for item in digest.items:
            if not self._is_important(item):
                continue
            key = self._key(item)
            if key in existing:
                continue
            records.append(self._record(item, key))
            existing.add(key)
            changed = True
        if changed:
            self._save_today(records, now)
        return self.enrich_digest(digest, now=now)

    def enrich_digest(self, digest: NewsDigest, now: datetime | None = None, recent_minutes: int = 60) -> NewsDigest:
        if "daily_news_flash_context_attached" in digest.warnings:
            return digest
        records = self._load_today(now)
        if not records and not digest.items:
            return digest
        now_utc = self._now_utc(now)
        recent_cutoff = now_utc - timedelta(minutes=recent_minutes)
        recent_lines = [
            self._format_item(item)
            for item in digest.items
            if item.published_at.astimezone(UTC) >= recent_cutoff
        ][:12]
        today_lines = [self._format_record(record) for record in records[:30]]
        sections: list[str] = []
        if recent_lines:
            sections.append("最近1小时快讯：\n" + "\n".join(recent_lines))
        if today_lines:
            sections.append("今日重点快讯记忆：\n" + "\n".join(today_lines))
        if digest.summary:
            sections.append("当前新闻摘要：\n" + digest.summary)
        if not sections:
            return digest
        summary = "\n\n".join(sections)
        warnings = list(dict.fromkeys([*digest.warnings, "daily_news_flash_context_attached"]))
        return digest.model_copy(update={"summary": summary, "warnings": warnings})

    def context_summary(self, now: datetime | None = None, limit: int = 30) -> str:
        records = self._load_today(now)
        if not records:
            return ""
        lines = [self._format_record(record) for record in records[:limit]]
        return "今日重点快讯记忆：\n" + "\n".join(lines)

    def today_path(self, now: datetime | None = None) -> Path:
        local_now = self._now_utc(now).astimezone(self.timezone)
        return self.root / f"{local_now:%Y-%m-%d}.json"

    def _load_today(self, now: datetime | None = None) -> list[dict[str, Any]]:
        path = self.today_path(now)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        data.sort(key=lambda row: (row.get("importance", 0), row.get("published_at", "")), reverse=True)
        return data

    def _save_today(self, records: list[dict[str, Any]], now: datetime | None = None) -> None:
        path = self.today_path(now)
        path.parent.mkdir(parents=True, exist_ok=True)
        records.sort(key=lambda row: (row.get("importance", 0), row.get("published_at", "")), reverse=True)
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    def _is_important(self, item: NewsItem) -> bool:
        text = f"{item.title} {item.summary} {item.category}".lower()
        if any(word in text for word in SHORT_NOISE_KEYWORDS):
            return False
        return (
            item.credibility >= 0.80
            or sum(1 for keyword in LONG_IMPACT_KEYWORDS if keyword in text) >= 1
            or item.category.lower() in {"macro", "politics", "geopolitical", "regulation"}
        )

    def _record(self, item: NewsItem, key: str) -> dict[str, Any]:
        text = f"{item.title} {item.summary} {item.category}".lower()
        importance = sum(1 for keyword in LONG_IMPACT_KEYWORDS if keyword in text)
        if item.credibility >= 0.90:
            importance += 2
        return {
            "key": key,
            "title": item.title,
            "summary": item.summary or item.title,
            "source": item.source,
            "url": item.url,
            "category": item.category,
            "published_at": item.published_at.astimezone(UTC).isoformat(),
            "credibility": item.credibility,
            "importance": importance,
        }

    def _format_item(self, item: NewsItem) -> str:
        when = item.published_at.astimezone(self.timezone).strftime("%H:%M")
        summary = (item.summary or item.title).strip()
        return f"- {when} [{item.source}] {item.title}：{summary}"

    def _format_record(self, record: dict[str, Any]) -> str:
        when = self._parse_dt(str(record.get("published_at") or "")).astimezone(self.timezone).strftime("%H:%M")
        source = str(record.get("source") or "")
        title = str(record.get("title") or "").strip()
        summary = str(record.get("summary") or title).strip()
        return f"- {when} [{source}] {title}：{summary}"

    def _key(self, item: NewsItem) -> str:
        raw = item.url or item.title
        text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", raw.lower())
        return text[:160]

    def _parse_dt(self, value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            return datetime.now(UTC)

    def _now_utc(self, now: datetime | None = None) -> datetime:
        if now is None:
            return datetime.now(UTC)
        if now.tzinfo is None:
            return now.replace(tzinfo=UTC)
        return now.astimezone(UTC)
