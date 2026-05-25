from __future__ import annotations

import asyncio
import email.utils
import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from html import unescape
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from ai_quant_trader.core.models import Alignment, NewsDigest, NewsItem

logger = logging.getLogger(__name__)


SOURCE_NAMES = {
    "coindesk.com": "CoinDesk",
    "cointelegraph.com": "Cointelegraph",
    "federalreserve.gov": "美联储",
    "bls.gov": "美国劳工统计局",
    "whitehouse.gov": "白宫",
    "treasury.gov": "美国财政部",
    "bea.gov": "美国经济分析局",
    "sec.gov": "美国证监会",
    "investing.com": "英为财情",
    "forexlive.com": "ForexLive",
    "marketwatch.com": "市场观察",
    "cnbc.com": "CNBC",
    "jin10.com": "金十数据",
}

SOURCE_DISPLAY_NAMES = {
    "CoinDesk": "加密财经媒体",
    "Cointelegraph": "加密新闻媒体",
    "ForexLive": "外汇快讯",
    "CNBC": "财经媒体CNBC",
    "MarketWatch": "市场观察",
    "Investing.com": "英为财情",
    "Federal Reserve": "美联储",
    "Fed": "美联储",
}


IMPORTANT_KEYWORDS = (
    "fed", "fomc", "federal reserve", "powell", "rate cut", "rate hike", "interest rate",
    "cpi", "ppi", "pce", "inflation", "nonfarm", "payroll", "jobs", "unemployment",
    "gdp", "recession", "treasury", "dollar", "dxy", "yield", "liquidity", "debt",
    "white house", "president", "trump", "tariff", "sanction", "war", "conflict",
    "attack", "ceasefire", "geopolitical", "oil", "gold", "government shutdown",
    "sec", "etf", "lawsuit", "stablecoin", "bitcoin", "ethereum", "crypto regulation",
    "hack", "bankruptcy", "default", "crisis",
    "美联储", "鲍威尔", "降息", "加息", "利率", "通胀", "非农", "初请", "美元", "美债",
    "收益率", "原油", "黄金", "债务上限", "政府关门", "总统", "白宫", "财政部",
    "制裁", "战争", "冲突", "袭击", "停火", "监管", "稳定币", "交易所",
)


ACTOR_PATTERNS = (
    ("鲍威尔", ("powell", "鲍威尔")),
    ("美联储", ("fed", "fomc", "federal reserve", "美联储", "联储")),
    ("白宫", ("white house", "白宫")),
    ("美国总统", ("president", "trump", "biden", "美国总统", "特朗普", "拜登")),
    ("美国财政部", ("treasury", "财政部")),
    ("美国劳工统计局", ("bls", "劳工统计局")),
    ("美国经济分析局", ("bea", "经济分析局")),
    ("美国证监会", ("sec", "证监会")),
    ("OPEC或能源市场", ("opec", "oil", "crude", "原油", "欧佩克")),
    ("加密监管或交易所", ("bitcoin", "ethereum", "crypto", "stablecoin", "exchange", "比特币", "以太坊", "稳定币", "交易所")),
)


EN_ZH_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.I), replacement)
    for pattern, replacement in [
        (r"\bFederal Reserve\b|\bFOMC\b|\bFed\b", "美联储"),
        (r"\bFed Chair Jerome Powell\b|\bJerome Powell\b|\bPowell\b", "鲍威尔"),
        (r"\bDonald Trump\b|\bTrump\b", "特朗普"),
        (r"\bWhite House\b", "白宫"),
        (r"\bU\.S\.\b|\bUS\b", "美国"),
        (r"\bIran\b", "伊朗"),
        (r"\bEuropean\b", "欧洲"),
        (r"\bEurope\b", "欧洲"),
        (r"\bcautionary mood\b", "谨慎情绪"),
        (r"\bmarkets? wrap\b", "市场综述"),
        (r"\bmarkets?\b", "市场"),
        (r"\bwaive\b|\bwaiver\b", "豁免"),
        (r"\breportedly\b", "据报道"),
        (r"\bheadlines?\b", "要闻"),
        (r"\bprice fewer\b", "降低定价"),
        (r"\bprice\b", "定价"),
        (r"\bTreasury Department\b|\bTreasury\b", "美国财政部"),
        (r"\bSEC\b", "美国证监会"),
        (r"\bCPI\b", "CPI"),
        (r"\bPPI\b", "PPI"),
        (r"\bPCE\b", "PCE"),
        (r"\bcore PCE\b", "核心PCE"),
        (r"\binflation\b", "通胀"),
        (r"\binterest rates?\b|\brates?\b", "利率"),
        (r"\bhikes? on the radar\b", "加息风险升温"),
        (r"\bholds? at\b", "维持在"),
        (r"\bhigher\b", "更高"),
        (r"\bmay\b", "可能"),
        (r"\brisk assets?\b", "风险资产"),
        (r"\bretail sales\b", "零售销售"),
        (r"\btraders?\b", "交易员"),
        (r"\breduced bets on\b", "降低对"),
        (r"\brate cuts?\b", "降息"),
        (r"\brate hikes?\b", "加息"),
        (r"\bsaid\b|\bsays\b|\bstated\b", "表示"),
        (r"\bwarned\b|\bwarns\b", "警告"),
        (r"\bsignaled\b|\bsignals\b", "暗示"),
        (r"\bannounced\b|\bannounces\b", "宣布"),
        (r"\bapproved\b|\bapproves\b", "批准"),
        (r"\brejected\b|\brejects\b", "拒绝"),
        (r"\brises\b|\brose\b|\bsurged\b|\bjumped\b", "上涨"),
        (r"\bfalls\b|\bfell\b|\bdropped\b|\bslumped\b", "下跌"),
        (r"\bgovernment shutdown\b", "政府关门"),
        (r"\bdebt ceiling\b", "债务上限"),
        (r"\bsanctions?\b", "制裁"),
        (r"\bwar\b|\bconflict\b", "地缘冲突"),
        (r"\battack\b", "袭击"),
        (r"\boil\b|\bcrude\b|\bWTI\b|\bBrent\b", "原油"),
        (r"\bdollar\b|\bDXY\b|\bUSD\b", "美元"),
        (r"\bTreasury yields?\b|\byields?\b", "美债收益率"),
        (r"\bBitcoin\b|\bBTC\b", "比特币"),
        (r"\bEthereum\b|\bEther\b|\bETH\b", "以太坊"),
        (r"\bcrypto\b", "加密市场"),
        (r"\bstablecoin\b", "稳定币"),
        (r"\binflows?\b", "资金流入"),
        (r"\boutflows?\b", "资金流出"),
        (r"\bETF\b", "ETF"),
        (r"\bGDP\b", "GDP"),
        (r"\bcontracts?\b|\bcontracted\b|\bcontraction\b", "收缩"),
        (r"\bgrows?\b|\bgrowth\b|\bexpanded\b|\bexpands?\b", "增长"),
        (r"\bweighs? on\b", "拖累"),
        (r"\binvestors?\b", "投资者"),
        (r"\bmarket\b", "市场"),
    ]
)


DATA_PATTERN = re.compile(
    r"(?i)(?:"
    r"(?:CPI|PPI|PCE|GDP|DXY|WTI|Brent|yield|unemployment|payrolls?|nonfarm|fed funds|通胀|利率|收益率|原油|黄金|非农|初请|核心PCE)"
    r"[^。；;,.，]*?(?:\d+(?:\.\d+)?\s?%|\d+\s?(?:bp|bps|个基点)|[$€¥]?\d+(?:\.\d+)?\s?(?:bn|billion|m|million|万|亿))"
    r"|(?:\d+(?:\.\d+)?\s?%|\d+\s?(?:bp|bps|个基点)|[$€¥]?\d+(?:\.\d+)?\s?(?:bn|billion|m|million|万|亿))"
    r"[^。；;,.，]*?(?:CPI|PPI|PCE|GDP|DXY|WTI|Brent|yield|unemployment|payrolls?|nonfarm|fed funds|通胀|利率|收益率|原油|黄金|非农|初请|核心PCE)"
    r")"
)


class NewsCollector:
    """公开新闻采集器。

    只读取公开 RSS 和可访问网页，不绕过登录、付费墙、验证码或反爬限制。
    摘要层只抽取事实，不输出宏观空话。
    """

    def __init__(self, rss_sources: list[str], scrape_sources: list[str], max_age_hours: int = 6, jin10_enabled: bool = False):
        self.rss_sources = rss_sources
        self.scrape_sources = scrape_sources
        self.max_age = timedelta(hours=max_age_hours)
        self.jin10_enabled = jin10_enabled

    async def collect(self) -> NewsDigest:
        items: list[NewsItem] = []
        warnings: list[str] = []
        if self.jin10_enabled:
            try:
                items.extend(await asyncio.to_thread(self._fetch_jin10_public_flash_sync))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"jin10_error:{type(exc).__name__}")

        rss_results = await asyncio.gather(
            *(asyncio.to_thread(self._fetch_rss_sync, url) for url in self.rss_sources),
            return_exceptions=True,
        )
        for result in rss_results:
            if isinstance(result, Exception):
                warnings.append(f"rss_error:{type(result).__name__}")
            else:
                items.extend(result)

        page_results = await asyncio.gather(
            *(asyncio.to_thread(self._fetch_public_page_sync, url) for url in self.scrape_sources),
            return_exceptions=True,
        )
        for result in page_results:
            if isinstance(result, Exception):
                warnings.append(f"scrape_error:{type(result).__name__}")
            elif result:
                items.append(result)

        deduped = self._dedupe(self._fresh_items(items))
        for item in deduped:
            self._localize_item(item)
        deduped = [item for item in deduped if not self._is_low_information_item(item)]
        deduped.sort(
            key=lambda item: (item.source == "金十数据", item.published_at, self._importance_score(item), item.credibility),
            reverse=True,
        )

        summary = "；".join(item.summary for item in deduped[:8] if item.summary)
        return NewsDigest(
            items=deduped[:80],
            macro_risk_level=self._macro_risk(deduped),
            crypto_sentiment=self._sentiment_hint(deduped),
            summary=summary or "最近窗口内没有抓取到可用重点消息。",
            warnings=warnings,
        )

    def _fetch_jin10_public_flash_sync(self) -> list[NewsItem]:
        headers = {
            "User-Agent": "Mozilla/5.0 ai-quant-trader",
            "x-app-id": "bVBF4FyRTn5NJF5n",
            "x-version": "1.0.0",
        }
        output: list[NewsItem] = []
        seen_ids: set[str] = set()
        max_time: str | None = None
        for _ in range(6):
            params = {"channel": "-8200", "vip": "1"}
            if max_time:
                params["max_time"] = max_time
            payload = self._requests_payload("https://flash-api.jin10.com/get_flash_list", params=params, headers=headers)
            rows = payload.get("data") if isinstance(payload, dict) else []
            if not isinstance(rows, list) or not rows:
                break
            stop = False
            for row in rows:
                row_id = str(row.get("id") or row.get("time") or "")
                if row_id and row_id in seen_ids:
                    continue
                if row_id:
                    seen_ids.add(row_id)
                data = row.get("data") if isinstance(row, dict) else {}
                content = self._text(str(data.get("content") or data.get("title") or "")) if isinstance(data, dict) else ""
                if not content:
                    continue
                published_at = self._parse_jin10_datetime(str(row.get("time") or ""))
                if datetime.now(UTC) - published_at > self.max_age:
                    stop = True
                    continue
                title = self._clean_sentence(content, max_len=120)
                item = NewsItem(
                    title=title,
                    source="金十数据",
                    url=data.get("source_link") if isinstance(data, dict) else None,
                    published_at=published_at,
                    category=self._category(content),
                    credibility=0.86 if row.get("important") else 0.8,
                    summary=content,
                    raw_title=title,
                    raw_summary=content,
                )
                output.append(item)
            last_time = str(rows[-1].get("time") or "")
            if stop or not last_time or last_time == max_time:
                break
            max_time = last_time
        return output

    def _fetch_rss_sync(self, url: str) -> list[NewsItem]:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 ai-quant-trader"}, timeout=20)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        output: list[NewsItem] = []
        for elem in nodes[:50]:
            title = self._text(elem.findtext("title") or elem.findtext("{http://www.w3.org/2005/Atom}title"))
            raw_summary = self._text(
                elem.findtext("description")
                or elem.findtext("summary")
                or elem.findtext("{http://www.w3.org/2005/Atom}summary")
                or elem.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                or title
            )
            link = elem.findtext("link")
            atom_link = elem.find("{http://www.w3.org/2005/Atom}link")
            if not link and atom_link is not None:
                link = atom_link.attrib.get("href")
            published_at = self._parse_datetime(
                elem.findtext("pubDate")
                or elem.findtext("published")
                or elem.findtext("updated")
                or elem.findtext("{http://www.w3.org/2005/Atom}published")
                or elem.findtext("{http://www.w3.org/2005/Atom}updated")
                or elem.findtext("{http://purl.org/dc/elements/1.1/}date")
            )
            if not title:
                continue
            category = self._category(f"{title} {raw_summary}")
            item = NewsItem(
                title=title,
                source=self._source_name(url),
                url=link,
                published_at=published_at,
                category=category,
                credibility=self._credibility(url, category),
                summary=raw_summary,
                raw_title=title,
                raw_summary=raw_summary,
            )
            self._localize_item(item)
            output.append(item)
        return output

    def _fetch_public_page_sync(self, url: str) -> NewsItem | None:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 ai-quant-trader"}, timeout=20)
        if response.status_code in {401, 403, 429}:
            return None
        response.raise_for_status()
        title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, flags=re.I | re.S)
        title = self._text(title_match.group(1)) if title_match else ""
        if not title:
            return None
        category = self._category(title)
        item = NewsItem(
            title=title,
            source=self._source_name(url),
            url=url,
            published_at=datetime.now(UTC),
            category=category,
            credibility=self._credibility(url, category) * 0.85,
            summary=title,
            raw_title=title,
            raw_summary=title,
        )
        self._localize_item(item)
        return item

    def _localize_item(self, item: NewsItem) -> None:
        raw_title = self._repair_mojibake(self._text(item.title))
        raw_summary = self._repair_mojibake(self._text(item.summary))
        item.raw_title = item.raw_title or raw_title
        item.raw_summary = item.raw_summary or raw_summary
        if item.source == "金十数据":
            content = self._clean_sentence(raw_summary or raw_title, max_len=520)
            item.title = self._clean_sentence(raw_title or content, max_len=160)
            item.summary = content
            return
        item.title = self._jin10_style_headline(raw_title, raw_summary, item.source, item.category)
        item.summary = self._jin10_style_summary(raw_title, raw_summary, item.source, item.category)

    def _factual_summary(self, text: str | NewsItem, source: str | None = None) -> str:
        if isinstance(text, NewsItem):
            source = text.source
            text = f"{text.raw_title or text.title}。{text.raw_summary or text.summary}"
        cleaned = self._clean_sentence(self._repair_mojibake(str(text)), max_len=520)
        actor = self._extract_actor(cleaned, source or "")
        action = self._extract_action(cleaned)
        data = self._extract_data(cleaned)
        fact = self._to_chinese_detail(cleaned, fallback_source=source or actor)
        if data:
            return f"{actor}{action}，关键数据：{self._translate_text(data)}；细节：{fact}"
        return f"{actor}{action}，细节：{fact}"

    def _jin10_style_headline(self, title: str, summary: str, source: str, category: str) -> str:
        clauses = [clause for clause in self._translated_fact_clauses(f"{title}. {summary}", category) if not self._is_generic_fact(clause)]
        if clauses:
            return self._clean_sentence(clauses[0], max_len=120)
        return self._clean_sentence(f"{self._display_source(source)}：{self._fallback_fact_phrase(title or summary, category)}", max_len=120)

    def _jin10_style_summary(self, title: str, summary: str, source: str, category: str) -> str:
        clauses = self._translated_fact_clauses(f"{title}. {summary}", category)
        clauses = [clause for clause in clauses if not self._is_generic_fact(clause)]
        if not clauses:
            return self._clean_sentence(f"{self._display_source(source)}：{self._fallback_fact_phrase(title or summary, category)}", max_len=520)
        detail = "；".join(clauses[:5])
        return self._clean_sentence(f"{self._display_source(source)}：{detail}", max_len=520)

    def _translated_fact_clauses(self, text: str, category: str) -> list[str]:
        cleaned = self._normalise_flash_text(text)
        clauses: list[str] = []
        for clause in self._split_fact_clauses(cleaned):
            translated = self._translate_fact_clause(clause, category)
            if translated and translated not in clauses:
                clauses.append(translated)
        return clauses

    def _normalise_flash_text(self, text: str) -> str:
        text = self._repair_mojibake(self._text(text))
        text = re.sub(r"\b(Headlines|Markets|Market|News)\s*:", "；", text, flags=re.I)
        text = re.sub(
            r"(?<=[a-z0-9%])(?=(?:US|U\.S\.|Iran|Japan|Gold|WTI|S&P|DAX|CAC|BOE|BoE|IEA|Fed|Treasury|Bitcoin|Ethereum|Michael|Strategy|Trump|White House))",
            "；",
            text,
        )
        return text

    def _split_fact_clauses(self, text: str) -> list[str]:
        parts = re.split(r"[。；;\n]+|(?<=[.!?])\s+", text)
        output: list[str] = []
        for part in parts:
            clause = self._clean_sentence(part, max_len=260)
            if len(clause) >= 8:
                output.append(clause)
        return output[:12]

    def _translate_fact_clause(self, clause: str, category: str) -> str:
        original = clause
        stripped_original = clause.strip(" .!?？")
        lower = clause.lower()
        exact_patterns: tuple[tuple[str, str], ...] = (
            (r"(?i)^US reportedly to temporarily waive Iran sanctions in new draft proposal$", "据报道，美国将在新草案提案中暂时豁免伊朗制裁"),
            (r"(?i)^US to waive Iran sanctions\??$", "美国可能豁免伊朗制裁"),
            (r"(?i)^investingLive European markets wrap: A more cautionary mood$", "欧洲市场综述：市场情绪更趋谨慎"),
            (r"(?i)^Iran's ForMin Spokesperson says process of talks through Pakistani mediation is ongoing$", "伊朗外交部发言人表示，通过巴基斯坦调解的谈判进程仍在继续"),
            (r"(?i)^IEA chief warns that commercial oil inventories are depleting rapidly$", "IEA负责人警告，商业原油库存正在快速消耗"),
            (r"(?i)^Gold remains under pressure amid worries of Fed rate hikes, prolonged US-Iran stalemate$", "受美联储加息担忧和美伊僵局延长影响，黄金仍承压"),
            (r"(?i)^Surging bond yields are a major pain point for equities at the moment$", "债券收益率快速上行目前是股市主要压力点"),
            (r"(?i)^Wall Street mixed as Treasury yields ease, oil prices retreat$", "美债收益率回落、油价下跌，美股走势分化"),
            (r"(?i)^Ominous bond trades point to much higher rates$", "债券市场交易显示利率可能进一步大幅上行"),
            (r"(?i)^Saylor’s Strategy scoops \$2B Bitcoin, holdings reach 843,738 BTC$", "Michael Saylor旗下Strategy公司买入20亿美元比特币，持仓达到843,738枚BTC"),
            (r"(?i)^Fed hikes on the radar as inflation holds at 3\.1%$", "美联储加息风险升温，通胀维持在3.1%"),
            (r"(?i)^Investors warn higher rates may weigh on risk assets$", "投资者警告，更高利率可能拖累风险资产"),
        )
        for pattern, replacement in exact_patterns:
            if re.match(pattern, stripped_original):
                return replacement

        text = clause
        text = text.replace("Saylor?s", "Michael Saylor").replace("Saylor’s", "Michael Saylor").replace("’", "'")
        phrase_replacements: tuple[tuple[str, str], ...] = (
            (r"(?i)\binvestingLive\b", ""),
            (r"(?i)\bmarkets? wrap\b", "市场综述"),
            (r"(?i)\bcautionary mood\b", "情绪更趋谨慎"),
            (r"(?i)\b(\d+)-year Treasury yield\b", r"美国\1年期国债收益率"),
            (r"(?i)\bJapan's (\d+)-year yield\b", r"日本\1年期国债收益率"),
            (r"(?i)\bFed Chair Jerome Powell\b|\bJerome Powell\b|\bPowell\b", "鲍威尔"),
            (r"(?i)\bDonald Trump\b|\bTrump\b", "特朗普"),
            (r"(?i)\bWhite House\b", "白宫"),
            (r"(?i)\bFederal Reserve\b|\bFOMC\b|\bFed\b", "美联储"),
            (r"(?i)\bTreasury Department\b|\bTreasury\b", "美国财政部"),
            (r"(?i)\bU\.S\.\b|\bUS\b", "美国"),
            (r"(?i)\bIran\b", "伊朗"),
            (r"(?i)\bJapan\b", "日本"),
            (r"(?i)\bEurope\b|\bEuropean\b", "欧洲"),
            (r"(?i)\bGDP\b", "GDP"),
            (r"(?i)\bCPI\b", "CPI"),
            (r"(?i)\bPPI\b", "PPI"),
            (r"(?i)\bPCE\b", "PCE"),
            (r"(?i)\bcore PCE\b", "核心PCE"),
            (r"(?i)\binflation\b", "通胀"),
            (r"(?i)\bretail sales\b", "零售销售"),
            (r"(?i)\bnonfarm payrolls?\b", "非农就业"),
            (r"(?i)\bunemployment rate\b", "失业率"),
            (r"(?i)\bTreasury yields?\b|\bbond yields?\b|\byields?\b", "债券收益率"),
            (r"(?i)\binterest rates?\b|\brates?\b", "利率"),
            (r"(?i)\brate cuts?\b", "降息"),
            (r"(?i)\brate hikes?\b", "加息"),
            (r"(?i)\boil prices?\b|\bcrude\b|\bWTI crude\b|\bWTI\b|\bBrent\b", "原油"),
            (r"(?i)\bgold\b", "黄金"),
            (r"(?i)\bdollar\b|\bDXY\b|\bUSD\b", "美元"),
            (r"(?i)\bS&P 500 futures\b", "标普500期货"),
            (r"(?i)\bDAX\b", "德国DAX指数"),
            (r"(?i)\bCAC 40\b", "法国CAC40指数"),
            (r"(?i)\bBitcoin\b|\bBTC\b", "比特币"),
            (r"(?i)\bEthereum\b|\bEther\b|\bETH\b", "以太坊"),
            (r"(?i)\bMichael Saylor\b|\bSaylor’s\b|\bSaylor\b", "Michael Saylor"),
            (r"(?i)\bStrategy\b", "Strategy公司"),
            (r"(?i)\bETF\b", "ETF"),
            (r"(?i)\bstablecoin\b", "稳定币"),
            (r"(?i)\bsanctions?\b", "制裁"),
            (r"(?i)\btariffs?\b", "关税"),
            (r"(?i)\bwar\b|\bconflict\b|\bstalemate\b", "僵局"),
            (r"(?i)\battack\b", "袭击"),
            (r"(?i)\bceasefire\b", "停火"),
            (r"(?i)\breportedly\b", "据报道"),
            (r"(?i)\bsaid\b|\bsays\b|\bstated\b", "表示"),
            (r"(?i)\bwarned\b|\bwarns\b", "警告"),
            (r"(?i)\bsignaled\b|\bsignals\b", "暗示"),
            (r"(?i)\bannounced\b|\bannounces\b", "宣布"),
            (r"(?i)\bapproved\b|\bapproves\b", "批准"),
            (r"(?i)\brejected\b|\brejects\b", "拒绝"),
            (r"(?i)\bbought\b|\bbuy\b|\bbuying\b", "买入"),
            (r"(?i)\bscoops?\b", "买入"),
            (r"(?i)\breach(?:es|ed)?\b", "达到"),
            (r"(?i)\btouch(?:es|ed)?\b", "触及"),
            (r"(?i)\brises\b|\brose\b|\bsurged\b|\bjumped\b", "上涨"),
            (r"(?i)\bfalls\b|\bfell\b|\bdropped\b|\bslumped\b|\bretreat(?:s|ed)?\b", "下跌"),
            (r"(?i)\bmixed\b", "涨跌不一"),
            (r"(?i)\bease(?:s|d)?\b", "回落"),
            (r"(?i)\bunder pressure\b", "承压"),
            (r"(?i)\bhighest in a year\b", "一年高位"),
            (r"(?i)\brecord\b", "纪录高位"),
            (r"(?i)\bcommercial oil inventories\b", "商业原油库存"),
            (r"(?i)\bdepleting rapidly\b", "快速消耗"),
            (r"(?i)\bholdings?\b", "持仓"),
            (r"(?i)\bpurchase\b", "买入"),
            (r"(?i)\bfunded\b", "提供资金"),
            (r"(?i)\bacquisition\b", "收购"),
            (r"(?i)\bWall Street\b", "华尔街"),
            (r"(?i)\bfinancial markets?\b|\bmarkets?\b", "市场"),
            (r"(?i)\binvestors?\b", "投资者"),
            (r"(?i)\btraders?\b", "交易员"),
            (r"(?i)\blabor-market weakness\b", "劳动力市场走弱"),
            (r"(?i)\bwidespread adoption of AI practices in the workplace\b", "AI在职场广泛采用"),
            (r"(?i)\blower bond yields\b", "更低的债券收益率"),
            (r"(?i)\blower interest rates\b", "更低利率"),
            (r"(?i)\bwill eventually mean\b", "最终可能意味着"),
            (r"(?i)\bThis chart shows why\b", "图表显示"),
            (r"(?i)\bcontends\b", "认为"),
            (r"(?i)\bcould peak near\b", "可能在附近见顶："),
            (r"(?i)\bin coming weeks\b", "未来数周"),
            (r"(?i)\bchance to buy stocks\b", "可能提供买入股票的机会"),
            (r"(?i)\bgives up all of May's gains\b", "回吐5月以来全部涨幅"),
            (r"(?i)\bslipping below\b", "跌破"),
            (r"(?i)\bnot lifting crypto spirits or prices\b", "未能提振加密市场情绪或价格"),
        )
        for pattern, replacement in phrase_replacements:
            text = re.sub(pattern, replacement, text)

        text = re.sub(r"(?i)\bQ([1-4])\b", r"第\1季度", text)
        text = re.sub(r"(?i)\$(\d+(?:\.\d+)?)B\b", r"\1十亿美元", text)
        text = re.sub(r"(?i)\b(\d+(?:\.\d+)?)\s*billion\b", r"\1十亿", text)
        text = re.sub(r"(?i)\b(\d+(?:\.\d+)?)\s*million\b", r"\1百万", text)
        text = re.sub(r"(?i)\b(\d+)\s?bp(s)?\b", r"\1个基点", text)
        text = self._strip_untranslated_english(text)
        text = self._clean_sentence(text, max_len=180)
        if self._fact_clause_is_useful(text):
            return text
        fallback = self._fallback_fact_phrase(original, category if category else self._category(lower))
        return "" if self._is_generic_fact(fallback) else fallback

    def _is_generic_fact(self, text: str) -> bool:
        return text in {"宏观金融消息更新", "政治或地缘风险消息更新", "加密市场消息更新"} or "相关消息更新" in text

    def _is_low_information_item(self, item: NewsItem) -> bool:
        if item.source == "金十数据":
            return False
        text = f"{item.title} {item.summary}"
        generic_markers = (
            "宏观金融消息更新",
            "政治或地缘风险消息更新",
            "加密市场消息更新",
            "关键数据：关键数据",
        )
        if any(marker in text for marker in generic_markers):
            return True
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        has_data = bool(re.search(r"\d+(?:\.\d+)?\s?%|\$?\d+", text))
        return chinese_chars < 10 and not has_data

    def _fact_clause_is_useful(self, text: str) -> bool:
        if not text:
            return False
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        has_data = bool(re.search(r"\d+(?:\.\d+)?\s?%|\$?\d+(?:,\d{3})*(?:\.\d+)?", text))
        generic_markers = ("利率路径和美联储政策预期变化", "消息更新", "相关快讯")
        return chinese_chars >= 6 and not any(marker in text for marker in generic_markers) and (has_data or chinese_chars >= 12)

    def _fallback_fact_phrase(self, text: str, category: str) -> str:
        data = self._extract_data(text)
        lowered = text.lower()
        if "trump" in lowered or "tariff" in lowered:
            return self._clean_sentence(f"特朗普或白宫相关消息更新" + (f"，关键数据：{data}" if data else ""), max_len=160)
        if "iran" in lowered or "sanction" in lowered:
            return self._clean_sentence(f"伊朗或制裁相关消息更新" + (f"，关键数据：{data}" if data else ""), max_len=160)
        if "gdp" in lowered:
            return self._clean_sentence(f"GDP数据发布" + (f"，关键数据：{data}" if data else ""), max_len=160)
        if "bitcoin" in lowered or "btc" in lowered:
            return self._clean_sentence(f"比特币市场消息更新" + (f"，关键数据：{data}" if data else ""), max_len=160)
        if data:
            return self._clean_sentence(f"关键数据：{data}", max_len=160)
        return {"macro": "宏观金融消息更新", "politics": "政治或地缘风险消息更新"}.get(category, "加密市场消息更新")

    def _detailed_chinese_summary(self, title: str, summary: str, source: str) -> str:
        title_cn = self._translate_text(title, max_len=220)
        summary_cn = self._translate_text(summary, max_len=420)
        parts = [part for part in (title_cn, summary_cn) if part]
        if len(parts) >= 2 and parts[1] == parts[0]:
            parts = parts[:1]
        detail = "。".join(parts)
        actor = self._extract_actor(detail, source)
        data = self._extract_data(detail)
        if data and data not in detail[:120]:
            detail = f"{detail}。关键数据：{data}"
        if self._has_long_english(detail):
            return self._pure_chinese_fact(detail, actor, self._category(detail), max_len=520)
        return self._clean_sentence(f"{actor}：{self._strip_untranslated_english(detail)}", max_len=520)

    def _to_chinese_headline(self, text: str, source: str, category: str) -> str:
        translated = self._translate_text(text)
        data = self._extract_data(translated)
        actor = self._extract_actor(translated, source)
        if self._has_long_english(translated):
            return self._pure_chinese_fact(translated, actor, category, max_len=120)
        return self._clean_sentence(self._strip_untranslated_english(translated), max_len=90)

    def _to_chinese_detail(self, text: str, fallback_source: str) -> str:
        translated = self._translate_text(text, max_len=520)
        if self._has_long_english(translated):
            return self._pure_chinese_fact(translated, self._display_source(fallback_source), self._category(translated), max_len=520)
        return self._clean_sentence(self._strip_untranslated_english(translated), max_len=520)

    def _translate_text(self, value: str, max_len: int = 220) -> str:
        text = self._repair_mojibake(self._text(value))
        for pattern, replacement in EN_ZH_REPLACEMENTS:
            text = pattern.sub(replacement, text)
        return self._clean_sentence(text, max_len=max_len)

    def _extract_actor(self, text: str, fallback: str) -> str:
        lowered = text.lower()
        for actor, aliases in ACTOR_PATTERNS:
            if any(alias in lowered or alias in text for alias in aliases):
                return actor
        return self._display_source(fallback) or "公开消息源"

    def _display_source(self, source: str) -> str:
        if source in SOURCE_DISPLAY_NAMES:
            return SOURCE_DISPLAY_NAMES[source]
        if re.search(r"\b[A-Za-z]{4,}\b", source or ""):
            return "公开消息源"
        return source

    def _extract_action(self, text: str) -> str:
        lowered = text.lower()
        if any(word in text for word in ("表示", "称", "警告", "暗示", "宣布", "批准", "拒绝", "起诉", "调查", "制裁")):
            return "表示"
        if any(word in lowered for word in ("said", "says", "warn", "signal", "announce", "approve", "reject", "sue", "investigate", "sanction")):
            return "表示"
        if any(word in lowered for word in ("up", "down", "higher", "lower", "rise", "fall", "surge", "drop")):
            return "出现变化"
        return "发布消息"

    def _extract_data(self, text: str) -> str:
        snippets: list[str] = []
        for match in DATA_PATTERN.finditer(text):
            snippets.append(self._clean_sentence(match.group(0), max_len=90))

        value_pattern = re.compile(
            r"(?i)(?:\d+(?:\.\d+)?\s?%|\d+\s?(?:bp|bps|个基点)|[$€¥]?\d+(?:\.\d+)?\s?(?:bn|billion|m|million|亿|万亿))"
        )
        for match in value_pattern.finditer(text):
            value = match.group(0).strip()
            if any(value in snippet for snippet in snippets):
                continue
            start = max(0, match.start() - 18)
            end = min(len(text), match.end() + 18)
            snippets.append(self._clean_sentence(text[start:end], max_len=70))

        deduped: list[str] = []
        for snippet in snippets:
            translated = self._strip_untranslated_english(self._translate_text(snippet, max_len=90))
            if translated and translated not in deduped:
                deduped.append(translated)
        return "；".join(deduped[:4])

    def _clean_sentence(self, value: str, max_len: int = 140) -> str:
        value = re.sub(r"\s+", " ", value).strip(" ，,。；;:?？")
        value = re.sub(r"\[[^\]]+\]|\([Rr]euters\)|\([Aa]P\)", "", value).strip()
        return value[:max_len].rstrip(" ，,。；;:?？")

    def _pure_chinese_fact(self, text: str, actor: str, category: str, max_len: int) -> str:
        topics = self._topic_phrases(text, category)
        data = self._extract_data(text)
        body = "，".join(topics) if topics else {"macro": "宏观金融消息更新", "politics": "政治或地缘风险消息更新"}.get(category, "加密市场消息更新")
        if data:
            body = f"{body}；关键数据：{self._strip_untranslated_english(data)}"
        return self._clean_sentence(f"{actor}：{body}", max_len=max_len)

    def _topic_phrases(self, text: str, category: str) -> list[str]:
        lowered = text.lower()
        topics: list[str] = []
        if any(word in text for word in ("美联储", "利率", "降息", "加息")) or any(word in lowered for word in ("fed", "fomc", "rate")):
            topics.append("利率路径和美联储政策预期变化")
        if any(word in text for word in ("通胀", "CPI", "PPI", "PCE", "零售销售", "GDP")):
            topics.append("美国经济数据影响风险资产定价")
        if any(word in text for word in ("美债收益率", "收益率", "美元")):
            topics.append("美元和美债收益率扰动流动性预期")
        if any(word in text for word in ("制裁", "伊朗", "战争", "冲突", "关税", "白宫", "总统")):
            topics.append("政治或地缘风险可能改变避险情绪")
        if any(word in text for word in ("欧洲", "谨慎情绪", "市场综述")):
            topics.append("欧美市场情绪偏谨慎")
        if any(word in text for word in ("比特币", "以太坊", "ETF", "稳定币", "加密市场")):
            topics.append("加密市场资金情绪和监管预期变化")
        if not topics and category == "crypto":
            topics.append("加密市场资金情绪变化")
        return topics[:4]

    def _strip_untranslated_english(self, value: str) -> str:
        allowed = {"CPI", "PPI", "PCE", "GDP", "DXY", "ETF", "USDT", "BTC", "ETH", "SOL", "WTI"}
        def replace(match: re.Match[str]) -> str:
            token = match.group(0)
            return token if token.upper() in allowed else ""

        value = re.sub(r"\b[A-Za-z][A-Za-z0-9'’.-]*\b", replace, value)
        value = re.sub(r"\s+", " ", value)
        value = re.sub(r"\s+([，。；：？！])", r"\1", value)
        value = re.sub(r"[:：]\s*[。；,，]*", "：", value)
        return value.strip(" ，,。；;:")

    def _text(self, value: str | None) -> str:
        if not value:
            return ""
        return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", value))).strip()

    def _repair_mojibake(self, value: str) -> str:
        if not value:
            return ""
        repaired = value
        if any(token in repaired for token in ("å", "ç", "è", "æ", "ã", "â")):
            try:
                repaired = repaired.encode("latin1").decode("utf-8")
            except UnicodeError:
                pass
        return repaired.replace("â", "'").replace("â", "“").replace("â", "”").replace("â", "—")

    def _has_long_english(self, value: str) -> bool:
        words = re.findall(r"\b[A-Za-z]{4,}\b", value)
        allowed = {"USDT", "PCE", "CPI", "PPI", "GDP", "DXY", "ETF", "WTI", "CNBC"}
        return len([word for word in words if word.upper() not in allowed]) >= 3

    def _parse_datetime(self, value: str | None) -> datetime:
        if not value:
            return datetime.now(UTC)
        try:
            dt = email.utils.parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except (TypeError, ValueError):
            return datetime.now(UTC)

    def _parse_jin10_datetime(self, value: str) -> datetime:
        try:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)
        except ValueError:
            return datetime.now(UTC)

    def _requests_payload(self, url: str, params: dict[str, str], headers: dict[str, str]) -> dict[str, Any]:
        response = requests.get(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("news response is not a JSON object")
        return payload

    def _fresh_items(self, items: list[NewsItem]) -> list[NewsItem]:
        now = datetime.now(UTC)
        return [item for item in items if now - item.published_at <= self.max_age]

    def _dedupe(self, items: list[NewsItem]) -> list[NewsItem]:
        seen: set[str] = set()
        output: list[NewsItem] = []
        for item in sorted(items, key=lambda x: (x.credibility, self._importance_score(x), x.published_at), reverse=True):
            key = self._dedupe_key(item)
            if key and key not in seen:
                seen.add(key)
                output.append(item)
        return output

    def _dedupe_key(self, item: NewsItem) -> str:
        words = re.findall(r"[a-z0-9\u4e00-\u9fff]+", f"{item.title} {item.summary}".lower())
        important = [word for word in words if len(word) > 2 and word not in {"the", "and", "for", "with", "from", "says", "after"}]
        hits = [word for word in important if any(key in word or word in key for key in IMPORTANT_KEYWORDS)]
        if hits:
            return "kw:" + item.published_at.strftime("%Y%m%d%H") + ":" + "-".join(sorted(set(hits))[:8])
        return "title:" + "".join(words)[:120]

    def _source_name(self, url: str) -> str:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        for domain, name in SOURCE_NAMES.items():
            if domain in host:
                return name
        return host or url

    def _credibility(self, url: str, category: str) -> float:
        host = urlparse(url).netloc.lower()
        if any(domain in host for domain in ("federalreserve.gov", "bls.gov", "whitehouse.gov", "treasury.gov", "bea.gov", "sec.gov")):
            return 0.95
        if any(domain in host for domain in ("forexlive.com", "marketwatch.com", "cnbc.com", "investing.com")):
            return 0.76 if category in {"macro", "politics"} else 0.68
        if any(domain in host for domain in ("coindesk.com", "cointelegraph.com")):
            return 0.72
        if "jin10.com" in host:
            return 0.78
        return 0.55

    def _importance_score(self, item: NewsItem) -> int:
        text = f"{item.title} {item.summary} {item.category}".lower()
        return sum(1 for keyword in IMPORTANT_KEYWORDS if keyword in text)

    def _category(self, text: str) -> str:
        lowered = text.lower()
        if any(word in lowered for word in ["fed", "fomc", "inflation", "cpi", "ppi", "pce", "jobs", "gdp", "dollar", "rate", "treasury", "nonfarm", "yield", "oil", "gold", "美联储", "降息", "加息", "美元", "美债", "原油", "黄金"]):
            return "macro"
        if any(word in lowered for word in ["president", "white house", "war", "conflict", "sanction", "tariff", "attack", "government shutdown", "debt ceiling", "总统", "白宫", "战争", "冲突", "制裁", "政府关门", "债务上限"]):
            return "politics"
        return "crypto"

    def _macro_risk(self, items: list[NewsItem]) -> str:
        risk_words = ("war", "conflict", "sanction", "inflation", "rate hike", "tariff", "crisis", "attack", "government shutdown", "战争", "制裁", "通胀", "政府关门")
        count = sum(1 for item in items if any(word in f"{item.title} {item.summary}".lower() for word in risk_words))
        return "high" if count >= 3 else "medium" if count else "low"

    def _sentiment_hint(self, items: list[NewsItem]) -> Alignment:
        text = " ".join(f"{item.title} {item.summary}".lower() for item in items[:25])
        positive = sum(text.count(word) for word in ["etf", "inflow", "rally", "approval", "cut", "dovish", "降息", "批准", "资金流入"])
        negative = sum(text.count(word) for word in ["hack", "lawsuit", "outflow", "ban", "war", "hike", "hawkish", "sanction", "战争", "制裁", "加息", "资金流出"])
        if positive > negative + 1:
            return Alignment.ALIGNED
        if negative > positive + 1:
            return Alignment.CONFLICT
        return Alignment.NEUTRAL
