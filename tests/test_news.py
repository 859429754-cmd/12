from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone

from ai_quant_trader.app import TradingApp
from ai_quant_trader.core.models import NewsDigest, NewsItem
from ai_quant_trader.data.news import NewsCollector
from ai_quant_trader.data.news_memory import DailyNewsFlashStore, NewsMemoryStore
from ai_quant_trader.reporting.hourly import HourlyReportBuilder
from tests.test_console_api import write_config


def test_parse_rss_datetime() -> None:
    collector = NewsCollector([], [])
    dt = collector._parse_datetime("Tue, 12 May 2026 22:41:00 GMT")
    assert dt == datetime(2026, 5, 12, 22, 41, tzinfo=UTC)


def test_source_name_is_readable() -> None:
    collector = NewsCollector([], [])
    assert collector._source_name("https://www.federalreserve.gov/feeds/press_all.xml") == "美联储"


def test_news_factual_summary_avoids_macro_template() -> None:
    collector = NewsCollector([], [])
    item = NewsItem(
        title="Powell says rate cuts may be limited as core PCE remains at 2.8%",
        source="Federal Reserve",
        summary="Fed Chair Jerome Powell said officials may cut less than expected while core PCE inflation remains at 2.8%.",
    )
    summary = collector._factual_summary(item)
    assert "观察" not in summary
    assert "传导" not in summary
    assert "鲍威尔" in summary
    assert "2.8%" in summary


def test_news_collector_localizes_title_and_summary() -> None:
    collector = NewsCollector([], [])
    item = NewsItem(
        title="Fed hikes on the radar as inflation holds at 3.1%",
        source="Investing.com",
        summary="Investors warn higher rates may weigh on risk assets.",
        category="macro",
    )
    collector._localize_item(item)
    text = f"{item.title} {item.summary}"
    assert "美联储" in text
    assert "通胀" in text or "利率" in text
    assert "3.1%" in text
    assert "hikes on the radar" not in text


def test_news_localization_keeps_detailed_facts_instead_of_generic_template() -> None:
    collector = NewsCollector([], [])
    item = NewsItem(
        title="Treasury yields could peak near 5% as investors price fewer rate cuts",
        source="MarketWatch",
        summary="The 10-year Treasury yield rose to 4.72% after stronger retail sales data, while traders reduced bets on a June Fed cut.",
        category="macro",
    )
    collector._localize_item(item)
    text = f"{item.title} {item.summary}"
    assert "发布宏观金融相关快讯" not in text
    assert "消息涉及利率、通胀、美元、美债或经济数据变化" not in text
    assert "4.72%" in text
    assert "5%" in text
    assert not re.search(r"\b[A-Za-z]{4,}\b", item.title)
    assert not re.search(r"\b[A-Za-z]{4,}\b", item.summary)
    assert "retail sales" in item.raw_summary


def test_news_localization_outputs_jin10_style_specific_chinese_facts() -> None:
    collector = NewsCollector([], [])
    item = NewsItem(
        title="Trump says tariffs could stay as US GDP grows 2.4%",
        source="test",
        summary="The White House said Trump will review Iran sanctions after US GDP grew 2.4% in Q1.",
        category="politics",
    )
    collector._localize_item(item)
    text = f"{item.title} {item.summary}"
    assert "特朗普" in text
    assert "白宫" in text
    assert "伊朗" in text
    assert "制裁" in text
    assert "GDP" in text
    assert "2.4%" in text
    assert "发布政治或地缘风险快讯" not in text
    assert not re.search(r"\b[A-Za-z]{4,}\b", text)


def test_news_localization_preserves_geopolitical_headlines_and_market_data() -> None:
    collector = NewsCollector([], [])
    item = NewsItem(
        title="investingLive European markets wrap: A more cautionary mood; US to waive Iran sanctions?",
        source="ForexLive",
        summary=(
            "Headlines:US reportedly to temporarily waive Iran sanctions in new draft proposal"
            "Iran's ForMin Spokesperson says process of talks through Pakistani mediation is ongoing"
            "IEA chief warns that commercial oil inventories are depleting rapidly"
            "Markets:WTI crude down 0.2% to $100.80 on the day"
        ),
        category="politics",
    )
    collector._localize_item(item)
    text = f"{item.title} {item.summary}"
    assert "欧洲市场综述" in text
    assert "美国将在新草案提案中暂时豁免伊朗制裁" in text
    assert "伊朗外交部发言人" in text
    assert "商业原油库存" in text
    assert "0.2%" in text
    assert "$100.80" in text
    assert "利率路径和美联储政策预期变化" not in text
    assert not re.search(r"\b[A-Za-z]{4,}\b", text)


def test_jin10_public_flash_items_are_kept_as_chinese_timeline(monkeypatch) -> None:
    collector = NewsCollector([], [], jin10_enabled=True)
    calls: list[dict[str, str]] = []
    beijing_now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    def fake_payload(_url: str, params: dict[str, str], headers: dict[str, str]) -> dict:
        calls.append(params)
        assert headers["x-app-id"]
        return {
            "status": 200,
            "data": [
                {
                    "time": beijing_now,
                    "important": 1,
                    "data": {
                        "content": "美国国务院批准向比利时出售AGM-184联合打击导弹及相关设备，预计成本约为2.36亿美元。",
                        "source_link": "",
                    },
                }
            ],
        }

    monkeypatch.setattr(collector, "_requests_payload", fake_payload)
    items = collector._fetch_jin10_public_flash_sync()
    assert len(items) == 1
    assert items[0].source == "金十数据"
    assert "美国国务院批准" in items[0].summary
    assert "2.36亿美元" in items[0].summary
    assert items[0].published_at.tzinfo is not None
    assert calls[0]["channel"] == "-8200"
    collector._localize_item(items[0])
    assert items[0].summary == "美国国务院批准向比利时出售AGM-184联合打击导弹及相关设备，预计成本约为2.36亿美元"


async def test_collect_prioritizes_jin10_timeline_over_low_information_fallback(monkeypatch) -> None:
    collector = NewsCollector(["https://fallback.example/rss"], [], jin10_enabled=True)
    now = datetime.now(UTC)

    monkeypatch.setattr(
        collector,
        "_fetch_jin10_public_flash_sync",
        lambda: [
            NewsItem(
                title="伊朗总统：对话并不意味着屈服。",
                source="金十数据",
                published_at=now - timedelta(minutes=5),
                summary="伊朗总统：对话并不意味着屈服。",
                category="politics",
            )
        ],
    )
    monkeypatch.setattr(
        collector,
        "_fetch_rss_sync",
        lambda _url: [
            NewsItem(
                title="This chart shows why AI will eventually mean lower bond yields",
                source="MarketWatch",
                published_at=now,
                summary="Potential labor-market weakness following the widespread adoption of AI practices in the workplace should eventually lead to lower interest rates.",
                category="macro",
            )
        ],
    )
    digest = await collector.collect()
    assert digest.items[0].source == "金十数据"
    assert "伊朗总统" in digest.items[0].title
    assert all("宏观金融消息更新" not in item.title + item.summary for item in digest.items)


def test_hourly_news_report_uses_recent_items_only() -> None:
    builder = HourlyReportBuilder()
    now = datetime.now(UTC)
    news = NewsDigest(
        items=[
            NewsItem(title="Fed rate hike warning", source="test", published_at=now - timedelta(days=1), credibility=0.95, category="macro"),
            NewsItem(title="Powell says inflation remains sticky", source="test", published_at=now - timedelta(minutes=20), credibility=0.95, category="macro"),
        ]
    )
    report = "\n".join(builder.news_brief(news))
    assert "通胀" in report or "利率" in report or "鲍威尔" in report
    assert "rate hike warning" not in report


def test_news_memory_keeps_long_impact_and_drops_noise(tmp_path) -> None:
    now = datetime.now(UTC)
    memory = NewsMemoryStore(str(tmp_path / "news_memory.json"))
    digest = NewsDigest(
        items=[
            NewsItem(title="Fed signals rate cut path may change", source="Fed", published_at=now, credibility=0.95, category="macro"),
            NewsItem(title="European markets wrap technical analysis", source="Blog", published_at=now, credibility=0.7, category="macro"),
        ]
    )
    enriched = memory.update(digest)
    records = memory._load()
    assert len(records) == 1
    assert "Fed" in records[0]["title"]
    assert "7天" in enriched.summary or "长期影响" in enriched.summary


def test_daily_news_flash_store_records_today_and_enriches_signal_context(tmp_path) -> None:
    now = datetime(2026, 5, 21, 10, 30, tzinfo=UTC)
    store = DailyNewsFlashStore(str(tmp_path / "news_daily"))
    digest = NewsDigest(
        summary="当前窗口摘要",
        items=[
            NewsItem(
                title="美国GDP增长2.4%，白宫称将评估伊朗制裁豁免",
                source="金十数据",
                published_at=now - timedelta(minutes=20),
                credibility=0.95,
                category="macro",
                summary="美国GDP增长2.4%，白宫称将评估伊朗制裁豁免，WTI原油价格快速波动。",
            ),
            NewsItem(
                title="普通技术分析复盘",
                source="Blog",
                published_at=now,
                credibility=0.4,
                category="macro",
                summary="technical analysis recap",
            ),
        ],
    )

    enriched = store.update(digest, now=now)
    records = json.loads(store.today_path(now).read_text(encoding="utf-8"))

    assert len(records) == 1
    assert records[0]["source"] == "金十数据"
    assert "最近1小时快讯" in enriched.summary
    assert "今日重点快讯记忆" in enriched.summary
    assert "美国GDP增长2.4%" in enriched.summary
    assert "daily_news_flash_context_attached" in enriched.warnings


def test_trading_app_reads_cached_news_digest(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl", ["ETH/USDT:USDT"])
    app = TradingApp(str(config_path))
    digest = NewsDigest(
        summary="??GDP????????",
        items=[NewsItem(title="??GDP???????", source="????", summary="??GDP????????")],
    )
    app.store.insert("news_summaries", digest.model_dump(mode="json"))

    cached = app._latest_cached_news_digest()

    assert cached is not None
    assert cached.summary == "??GDP????????"
    assert cached.items[0].source == "????"
    app.store.close()
