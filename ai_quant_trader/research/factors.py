from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class FactorCategory(str, Enum):
    STRATEGY_TRIGGER = "strategy_trigger"
    TREND_MOMENTUM = "trend_momentum"
    VOLATILITY_RISK = "volatility_risk"
    VOLUME_LIQUIDITY = "volume_liquidity"
    ORDERFLOW = "orderflow"
    DENSE_ZONE = "dense_zone"
    PATTERN = "pattern"
    MARKET_REGIME = "market_regime"
    HIGHER_TIMEFRAME = "higher_timeframe"
    CROSS_ASSET = "cross_asset"
    DERIVATIVES = "derivatives"
    NEWS_MACRO = "news_macro"
    ONCHAIN = "onchain"
    EXECUTION = "execution"
    ACCOUNT_RISK = "account_risk"
    OUTCOME_ONLY = "outcome_only"


class FactorAvailability(str, Enum):
    BACKTESTABLE_NOW = "backtestable_now"
    BACKTESTABLE_WITH_BACKFILL = "backtestable_with_backfill"
    LIVE_ONLY_NEEDS_ARCHIVE = "live_only_needs_archive"
    NEEDS_NEW_DATA_SOURCE = "needs_new_data_source"
    FORBIDDEN_OUTCOME_LEAKAGE = "forbidden_outcome_leakage"


class FactorRole(str, Enum):
    PROFIT_EXPANSION = "profit_expansion"
    LOSS_SUPPRESSION = "loss_suppression"
    EXECUTION_QUALITY = "execution_quality"
    CONTEXT_QUALITY = "context_quality"
    HARD_RISK_GATE = "hard_risk_gate"
    RESEARCH_LABEL_ONLY = "research_label_only"


@dataclass(frozen=True)
class FactorSpec:
    name: str
    category: FactorCategory
    role: FactorRole
    availability: FactorAvailability
    source: str
    direction: str
    description: str
    lookahead_safe: bool = True
    current_field: str | None = None
    priority: int = 3

    @property
    def eligible_for_optimization(self) -> bool:
        return self.lookahead_safe and self.availability in {
            FactorAvailability.BACKTESTABLE_NOW,
            FactorAvailability.BACKTESTABLE_WITH_BACKFILL,
        }


FACTOR_LIBRARY: tuple[FactorSpec, ...] = (
    FactorSpec(
        "strategy_signal_strength",
        FactorCategory.STRATEGY_TRIGGER,
        FactorRole.PROFIT_EXPANSION,
        FactorAvailability.BACKTESTABLE_NOW,
        "TrendStrategy.technical_evidence",
        "higher supports larger tier",
        "Local KC+VOL+KDJ signal strength. AI cannot invent direction without this signal.",
        current_field="signal_strength",
        priority=1,
    ),
    FactorSpec(
        "kc_breakout_atr",
        FactorCategory.STRATEGY_TRIGGER,
        FactorRole.PROFIT_EXPANSION,
        FactorAvailability.BACKTESTABLE_NOW,
        "TrendStrategy.technical_evidence",
        "moderate higher supports; extreme higher can become overextension risk",
        "Breakout distance normalized by ATR.",
        current_field="breakout_atr",
        priority=2,
    ),
    FactorSpec(
        "volume_multiple",
        FactorCategory.VOLUME_LIQUIDITY,
        FactorRole.EXECUTION_QUALITY,
        FactorAvailability.BACKTESTABLE_NOW,
        "OHLCV",
        "higher supports breakout quality",
        "Closed-candle volume divided by VMA20.",
        current_field="volume_multiple",
        priority=1,
    ),
    FactorSpec(
        "atr_pct",
        FactorCategory.VOLATILITY_RISK,
        FactorRole.LOSS_SUPPRESSION,
        FactorAvailability.BACKTESTABLE_NOW,
        "OHLCV",
        "too high reduces tier",
        "ATR14 divided by close. Captures volatility and stop-distance pressure.",
        current_field="atr_pct",
        priority=2,
    ),
    FactorSpec(
        "kdj_confirmation",
        FactorCategory.TREND_MOMENTUM,
        FactorRole.PROFIT_EXPANSION,
        FactorAvailability.BACKTESTABLE_NOW,
        "TrendStrategy indicators",
        "aligned KDJ supports entry",
        "KDJ(9,3,3) direction gate used by the live trend strategy.",
        priority=1,
    ),
    FactorSpec(
        "kc_middle_exit_distance",
        FactorCategory.VOLATILITY_RISK,
        FactorRole.LOSS_SUPPRESSION,
        FactorAvailability.BACKTESTABLE_NOW,
        "OHLCV indicators",
        "closer to KC middle increases exit risk",
        "Distance from entry context to KC middle; useful for near-immediate exit risk.",
        priority=2,
    ),
    FactorSpec(
        "pattern_type",
        FactorCategory.PATTERN,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.BACKTESTABLE_NOW,
        "PatternDetector",
        "category-specific",
        "Local pattern type: triangle, wedge, rectangle, channel, double-top/bottom, etc.",
        current_field="pattern_type",
        priority=1,
    ),
    FactorSpec(
        "pattern_confirmation_score",
        FactorCategory.PATTERN,
        FactorRole.PROFIT_EXPANSION,
        FactorAvailability.BACKTESTABLE_NOW,
        "PatternDetector",
        "higher supports larger tier",
        "Pattern direction and confidence relative to local strategy direction.",
        current_field="pattern_aligned_score",
        priority=1,
    ),
    FactorSpec(
        "pattern_weakness_score",
        FactorCategory.PATTERN,
        FactorRole.LOSS_SUPPRESSION,
        FactorAvailability.BACKTESTABLE_NOW,
        "PatternDetector",
        "higher reduces tier",
        "Inverse of pattern confirmation; helps identify weak breakouts.",
        priority=1,
    ),
    FactorSpec(
        "dense_zone_position",
        FactorCategory.DENSE_ZONE,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.BACKTESTABLE_NOW,
        "DenseZoneAnalyzer",
        "category-specific",
        "Whether price is inside value area, above value, below value, or vacuum.",
        current_field="dense_position",
        priority=1,
    ),
    FactorSpec(
        "dense_zone_breakout_score",
        FactorCategory.DENSE_ZONE,
        FactorRole.PROFIT_EXPANSION,
        FactorAvailability.BACKTESTABLE_NOW,
        "DenseZoneAnalyzer",
        "higher supports migration/trend continuation",
        "Dense-zone breakout or migration quality.",
        current_field="dense_trend_score",
        priority=1,
    ),
    FactorSpec(
        "dense_range_score",
        FactorCategory.DENSE_ZONE,
        FactorRole.LOSS_SUPPRESSION,
        FactorAvailability.BACKTESTABLE_NOW,
        "DenseZoneAnalyzer",
        "higher reduces tier for trend strategy",
        "Dense-zone range behavior risk for a breakout strategy.",
        current_field="dense_range_score",
        priority=2,
    ),
    FactorSpec(
        "regime_trend_score",
        FactorCategory.MARKET_REGIME,
        FactorRole.PROFIT_EXPANSION,
        FactorAvailability.BACKTESTABLE_NOW,
        "RegimePatternAnalyzer",
        "higher supports larger tier",
        "Local market-state trend score.",
        current_field="regime_trend_score",
        priority=1,
    ),
    FactorSpec(
        "regime_range_score",
        FactorCategory.MARKET_REGIME,
        FactorRole.LOSS_SUPPRESSION,
        FactorAvailability.BACKTESTABLE_NOW,
        "RegimePatternAnalyzer",
        "higher reduces tier",
        "Range/chop risk for the trend strategy.",
        current_field="regime_range_score",
        priority=1,
    ),
    FactorSpec(
        "regime_risk_score",
        FactorCategory.MARKET_REGIME,
        FactorRole.HARD_RISK_GATE,
        FactorAvailability.BACKTESTABLE_NOW,
        "RegimePatternAnalyzer",
        "higher caps or blocks tier",
        "Composite regime danger score.",
        current_field="regime_risk_score",
        priority=1,
    ),
    FactorSpec(
        "higher_timeframe_alignment",
        FactorCategory.HIGHER_TIMEFRAME,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.BACKTESTABLE_NOW,
        "4h OHLCV context",
        "aligned supports; conflict caps",
        "4h structure alignment relative to 1h strategy side.",
        current_field="htf_alignment_score",
        priority=2,
    ),
    FactorSpec(
        "higher_timeframe_trend_strength",
        FactorCategory.HIGHER_TIMEFRAME,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.BACKTESTABLE_NOW,
        "4h OHLCV context",
        "strong aligned supports; strong conflict caps",
        "4h trend strength used together with alignment.",
        current_field="htf_trend_strength",
        priority=2,
    ),
    FactorSpec(
        "binance_aggtrade_participation",
        FactorCategory.ORDERFLOW,
        FactorRole.EXECUTION_QUALITY,
        FactorAvailability.BACKTESTABLE_WITH_BACKFILL,
        "Binance futures aggTrades archive",
        "higher supports execution quality",
        "Trade count, total quote, large trade quote and max trade quote before entry.",
        current_field="orderflow_confirmation_score",
        priority=1,
    ),
    FactorSpec(
        "directional_cvd_proxy",
        FactorCategory.ORDERFLOW,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.BACKTESTABLE_WITH_BACKFILL,
        "Binance futures aggTrades archive",
        "aligned direction supports; conflict caps",
        "Direction-adjusted CVD proxy before entry. Not a full order-book signal.",
        priority=2,
    ),
    FactorSpec(
        "large_trade_impulse",
        FactorCategory.ORDERFLOW,
        FactorRole.EXECUTION_QUALITY,
        FactorAvailability.BACKTESTABLE_WITH_BACKFILL,
        "Binance futures aggTrades archive",
        "higher supports impulse quality",
        "Large trade activity before entry.",
        priority=2,
    ),
    FactorSpec(
        "realtime_orderbook_imbalance",
        FactorCategory.ORDERFLOW,
        FactorRole.EXECUTION_QUALITY,
        FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE,
        "Binance/OKX/Bybit/Gate order book",
        "aligned liquidity supports; hostile liquidity caps",
        "Live order book imbalance and depth. Requires persistent archive before historical optimization.",
        priority=1,
    ),
    FactorSpec(
        "spread_bps",
        FactorCategory.EXECUTION,
        FactorRole.LOSS_SUPPRESSION,
        FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE,
        "Best bid/ask",
        "higher reduces tier",
        "Live spread cost. Needs historical quote archive for backtest.",
        priority=1,
    ),
    FactorSpec(
        "slippage_depth_score",
        FactorCategory.EXECUTION,
        FactorRole.LOSS_SUPPRESSION,
        FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE,
        "Order book depth",
        "weaker depth reduces tier",
        "Estimated market order slippage at intended notional.",
        priority=1,
    ),
    FactorSpec(
        "funding_rate",
        FactorCategory.DERIVATIVES,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.NEEDS_NEW_DATA_SOURCE,
        "Exchange funding history",
        "extreme funding can cap crowded side",
        "Perpetual funding pressure. Needs historical funding backfill.",
        priority=2,
    ),
    FactorSpec(
        "open_interest_change",
        FactorCategory.DERIVATIVES,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.NEEDS_NEW_DATA_SOURCE,
        "Exchange OI history",
        "direction-specific",
        "Open interest change around breakout. Useful for trend confirmation or squeeze risk.",
        priority=2,
    ),
    FactorSpec(
        "liquidation_cluster",
        FactorCategory.DERIVATIVES,
        FactorRole.LOSS_SUPPRESSION,
        FactorAvailability.NEEDS_NEW_DATA_SOURCE,
        "Liquidation feed",
        "nearby hostile cluster can cap",
        "Liquidation density near entry/stop area.",
        priority=3,
    ),
    FactorSpec(
        "btc_leader_alignment",
        FactorCategory.CROSS_ASSET,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE,
        "BTC OHLCV and market leader context",
        "aligned supports; conflict caps unless rotation lag",
        "BTC leader direction relative to ETH strategy side.",
        current_field="btc_leader_score",
        priority=1,
    ),
    FactorSpec(
        "eth_btc_rotation",
        FactorCategory.CROSS_ASSET,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE,
        "ETH/BTC relative strength",
        "rotation-lag can allow ETH catch-up",
        "Distinguishes BTC-led trend from ETH lagging rotation.",
        current_field="eth_btc_rotation_score",
        priority=1,
    ),
    FactorSpec(
        "total_market_breadth",
        FactorCategory.CROSS_ASSET,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.NEEDS_NEW_DATA_SOURCE,
        "Top exchange universe",
        "broad participation supports",
        "Market-wide breadth across top crypto assets.",
        priority=3,
    ),
    FactorSpec(
        "news_direction_alignment",
        FactorCategory.NEWS_MACRO,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE,
        "NewsContext + DeepSeek",
        "aligned supports; conflict blocks/caps",
        "Strategy-relative news direction: long+bullish or short+bearish.",
        current_field="news_direction_alignment_score",
        priority=1,
    ),
    FactorSpec(
        "news_execution_risk",
        FactorCategory.NEWS_MACRO,
        FactorRole.LOSS_SUPPRESSION,
        FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE,
        "NewsContext + DeepSeek",
        "higher reduces or blocks tier",
        "Volatility, headline uncertainty, policy shock and event risk independent of direction.",
        current_field="news_safety_score",
        priority=1,
    ),
    FactorSpec(
        "macro_liquidity_context",
        FactorCategory.NEWS_MACRO,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.NEEDS_NEW_DATA_SOURCE,
        "Macro calendar / DXY / yields / equities",
        "risk-on supports crypto longs; risk-off caps",
        "Macro liquidity and risk appetite context.",
        priority=3,
    ),
    FactorSpec(
        "exchange_flow_netflow",
        FactorCategory.ONCHAIN,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.NEEDS_NEW_DATA_SOURCE,
        "On-chain data vendor",
        "direction-specific",
        "Exchange inflow/outflow pressure. Not currently available.",
        priority=4,
    ),
    FactorSpec(
        "stablecoin_liquidity",
        FactorCategory.ONCHAIN,
        FactorRole.CONTEXT_QUALITY,
        FactorAvailability.NEEDS_NEW_DATA_SOURCE,
        "On-chain data vendor",
        "higher liquidity can support risk assets",
        "Stablecoin supply/flow condition. Not currently available.",
        priority=4,
    ),
    FactorSpec(
        "account_exposure_usage",
        FactorCategory.ACCOUNT_RISK,
        FactorRole.HARD_RISK_GATE,
        FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE,
        "Gate account snapshot",
        "higher caps tier",
        "Existing account leverage/exposure before new entry.",
        priority=1,
    ),
    FactorSpec(
        "native_stop_health",
        FactorCategory.ACCOUNT_RISK,
        FactorRole.HARD_RISK_GATE,
        FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE,
        "Gate native stop reconciliation",
        "bad health blocks new entries",
        "Whether native stop and exchange position state are reconciled.",
        priority=1,
    ),
    FactorSpec(
        "data_freshness",
        FactorCategory.EXECUTION,
        FactorRole.HARD_RISK_GATE,
        FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE,
        "readiness/data_health",
        "stale data blocks",
        "OHLCV/orderflow/news freshness gate.",
        priority=1,
    ),
    FactorSpec(
        "mae_pct",
        FactorCategory.OUTCOME_ONLY,
        FactorRole.RESEARCH_LABEL_ONLY,
        FactorAvailability.FORBIDDEN_OUTCOME_LEAKAGE,
        "post-trade ledger",
        "outcome label only",
        "Max adverse excursion. Useful for labels and audits, forbidden as an entry factor.",
        lookahead_safe=False,
        current_field="mae_pct",
        priority=1,
    ),
    FactorSpec(
        "mfe_pct",
        FactorCategory.OUTCOME_ONLY,
        FactorRole.RESEARCH_LABEL_ONLY,
        FactorAvailability.FORBIDDEN_OUTCOME_LEAKAGE,
        "post-trade ledger",
        "outcome label only",
        "Max favorable excursion. Useful for labels and audits, forbidden as an entry factor.",
        lookahead_safe=False,
        current_field="mfe_pct",
        priority=1,
    ),
    FactorSpec(
        "realized_pnl",
        FactorCategory.OUTCOME_ONLY,
        FactorRole.RESEARCH_LABEL_ONLY,
        FactorAvailability.FORBIDDEN_OUTCOME_LEAKAGE,
        "post-trade ledger",
        "outcome label only",
        "Realized trade PnL. It is the label, not an input factor.",
        lookahead_safe=False,
        current_field="pnl",
        priority=1,
    ),
)


def factors_by_category() -> dict[FactorCategory, list[FactorSpec]]:
    output: dict[FactorCategory, list[FactorSpec]] = {category: [] for category in FactorCategory}
    for factor in FACTOR_LIBRARY:
        output[factor.category].append(factor)
    return {category: sorted(items, key=lambda item: (item.priority, item.name)) for category, items in output.items() if items}


def factors_by_role() -> dict[FactorRole, list[FactorSpec]]:
    output: dict[FactorRole, list[FactorSpec]] = {role: [] for role in FactorRole}
    for factor in FACTOR_LIBRARY:
        output[factor.role].append(factor)
    return {role: sorted(items, key=lambda item: (item.priority, item.name)) for role, items in output.items() if items}


def factors_by_availability() -> dict[FactorAvailability, list[FactorSpec]]:
    output: dict[FactorAvailability, list[FactorSpec]] = {availability: [] for availability in FactorAvailability}
    for factor in FACTOR_LIBRARY:
        output[factor.availability].append(factor)
    return {
        availability: sorted(items, key=lambda item: (item.priority, item.name))
        for availability, items in output.items()
        if items
    }


def optimization_eligible_factors(factors: Iterable[FactorSpec] = FACTOR_LIBRARY) -> list[FactorSpec]:
    return sorted(
        [factor for factor in factors if factor.eligible_for_optimization],
        key=lambda item: (item.priority, item.category.value, item.name),
    )


def live_archive_required_factors(factors: Iterable[FactorSpec] = FACTOR_LIBRARY) -> list[FactorSpec]:
    return sorted(
        [factor for factor in factors if factor.availability == FactorAvailability.LIVE_ONLY_NEEDS_ARCHIVE],
        key=lambda item: (item.priority, item.category.value, item.name),
    )


def forbidden_outcome_factors(factors: Iterable[FactorSpec] = FACTOR_LIBRARY) -> list[FactorSpec]:
    return sorted(
        [factor for factor in factors if not factor.lookahead_safe or factor.availability == FactorAvailability.FORBIDDEN_OUTCOME_LEAKAGE],
        key=lambda item: (item.priority, item.name),
    )
