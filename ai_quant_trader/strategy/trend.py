from __future__ import annotations

import math
from typing import Any

import pandas as pd

from ai_quant_trader.core.models import (
    PositionSnapshot,
    SignalAction,
    StrategySignal,
)
from ai_quant_trader.core.models import TrendStrategyConfig
from ai_quant_trader.strategy.base import BaseStrategy
from ai_quant_trader.strategy.indicators import atr, ema, kdj, keltner_channel, sma


def normalize_trend_config(config: TrendStrategyConfig) -> TrendStrategyConfig:
    if config.variant == "no_volume":
        return config.model_copy(update={"use_volume_filter": False})
    return config.model_copy(update={"use_volume_filter": True})


class TrendStrategy(BaseStrategy):
    """趋势策略：89 EMA + KC(20, 2.8) + 20 VMA。

    该策略只负责提出技术面候选信号，不直接决定下单。
    """

    def __init__(self, config: TrendStrategyConfig):
        self.config = normalize_trend_config(config)

    def add_indicators(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        cfg = self.config
        df["ema_89"] = ema(df["close"], cfg.ema_length)
        kc = keltner_channel(df, cfg.kc_length, cfg.kc_scalar, cfg.atr_length)
        df = pd.concat([df, kc], axis=1)
        df["vma_20"] = sma(df["volume"], cfg.vma_length)
        df["atr"] = atr(df, cfg.atr_length)
        if cfg.momentum_filter == "kdj":
            df = pd.concat([df, kdj(df, cfg.kdj_length, cfg.kdj_k_smooth, cfg.kdj_d_smooth)], axis=1)
        return df

    def keltner_columns(self) -> tuple[str, str, str]:
        cfg = self.config
        return (
            f"KCUe_{cfg.kc_length}_{cfg.kc_scalar}",
            f"KCMe_{cfg.kc_length}_{cfg.kc_scalar}",
            f"KCLe_{cfg.kc_length}_{cfg.kc_scalar}",
        )

    def warmup_candles(self) -> int:
        cfg = self.config
        lengths = [cfg.kc_length, cfg.atr_length]
        if cfg.use_ema_filter:
            lengths.append(cfg.ema_length)
        if cfg.use_volume_filter:
            lengths.append(cfg.vma_length)
        if cfg.momentum_filter == "kdj":
            lengths.append(cfg.kdj_length + cfg.kdj_k_smooth + cfg.kdj_d_smooth)
        return max(lengths) + 5

    def evaluate_action_from_indicators(
        self,
        df: pd.DataFrame,
        idx: int,
        position: PositionSnapshot,
    ) -> SignalAction:
        if not self.config.enabled:
            return SignalAction.HOLD
        if idx <= 0 or idx >= len(df):
            return SignalAction.HOLD
        cfg = self.config
        last = df.iloc[idx]
        prev = df.iloc[idx - 1]
        kcu, kcm, kcl = self.keltner_columns()
        required_last = [kcu, kcm, kcl]
        if cfg.use_ema_filter:
            required_last.append("ema_89")
        if cfg.use_volume_filter:
            required_last.append("vma_20")
        required_last.extend(self._momentum_required_columns())
        required_prev = [kcu, kcm, kcl]
        if any(math.isnan(float(last[col])) for col in required_last):
            return SignalAction.HOLD
        if any(math.isnan(float(prev[col])) for col in required_prev):
            return SignalAction.HOLD

        close = float(last["close"])
        prev_close = float(prev["close"])
        volume = float(last["volume"])
        vma = float(last["vma_20"])
        if cfg.use_volume_filter and vma <= 0:
            return SignalAction.HOLD
        ema_long_ok = close > float(last["ema_89"]) if cfg.use_ema_filter else True
        ema_short_ok = close < float(last["ema_89"]) if cfg.use_ema_filter else True
        volume_ok = volume > vma * cfg.volume_multiple if cfg.use_volume_filter else True
        momentum_long_ok, momentum_short_ok = self._momentum_filter_ok(last)

        long_condition = (
            prev_close <= float(prev[kcu])
            and close > float(last[kcu])
            and ema_long_ok
            and volume_ok
            and momentum_long_ok
        )
        short_condition = (
            prev_close >= float(prev[kcl])
            and close < float(last[kcl])
            and ema_short_ok
            and volume_ok
            and momentum_short_ok
        )
        if long_condition:
            return SignalAction.LONG
        if short_condition:
            return SignalAction.SHORT

        exit_long = position.qty > 0 and prev_close >= float(prev[kcm]) and close < float(last[kcm])
        exit_short = position.qty < 0 and prev_close <= float(prev[kcm]) and close > float(last[kcm])
        if exit_long:
            return SignalAction.EXIT_LONG
        if exit_short:
            return SignalAction.EXIT_SHORT
        return SignalAction.HOLD

    def generate_exit_signal(
        self,
        symbol: str,
        timeframe: str,
        current_price: float,
        action: SignalAction,
        technical_evidence: dict[str, Any],
    ) -> StrategySignal:
        return StrategySignal(
            symbol=symbol,
            timeframe=timeframe,
            action=action,
            current_price=current_price,
            signal_strength=0.5,
            technical_evidence=technical_evidence,
        )

    def generate_signal(
        self,
        symbol: str,
        timeframe: str,
        candles: pd.DataFrame,
        position: PositionSnapshot,
        equity: float,
        ai_multiplier: float,
        leverage: float = 4.0,
    ) -> StrategySignal:
        df = self.add_indicators(candles)
        cfg = self.config
        if not cfg.enabled:
            return StrategySignal(
                symbol=symbol,
                timeframe=timeframe,
                action=SignalAction.HOLD,
                current_price=float(df["close"].iloc[-1]) if len(df) else 0.0,
                technical_evidence={
                    "reason": "strategy_profile_disabled",
                    "profile_name": cfg.profile_name,
                    "strategy_enabled": False,
                },
            )
        min_rows = self.warmup_candles() + 2
        if len(df) < min_rows:
            return StrategySignal(
                symbol=symbol,
                timeframe=timeframe,
                action=SignalAction.HOLD,
                current_price=float(df["close"].iloc[-1]) if len(df) else 0.0,
                technical_evidence={"reason": "insufficient_candles"},
            )

        last = df.iloc[-1]
        prev = df.iloc[-2]
        kcu, kcm, kcl = self.keltner_columns()
        close = float(last["close"])
        prev_close = float(prev["close"])
        volume = float(last["volume"])
        vma = float(last["vma_20"])
        atr_value = float(last["atr"]) if not math.isnan(float(last["atr"])) else 0.0
        ema_long_ok = close > float(last["ema_89"]) if cfg.use_ema_filter else True
        ema_short_ok = close < float(last["ema_89"]) if cfg.use_ema_filter else True
        volume_ok = volume > vma * cfg.volume_multiple if cfg.use_volume_filter else True
        momentum_long_ok, momentum_short_ok = self._momentum_filter_ok(last)

        target_leverage = max(float(leverage or 0.0), 0.0)
        base_nominal = equity * self.config.position_fraction * target_leverage * ai_multiplier
        qty = max(base_nominal / close, 0.0) if close > 0 else 0.0

        long_condition = (
            prev_close <= float(prev[kcu])
            and close > float(last[kcu])
            and ema_long_ok
            and volume_ok
            and momentum_long_ok
        )
        short_condition = (
            prev_close >= float(prev[kcl])
            and close < float(last[kcl])
            and ema_short_ok
            and volume_ok
            and momentum_short_ok
        )
        exit_long = position.qty > 0 and prev_close >= float(prev[kcm]) and close < float(last[kcm])
        exit_short = position.qty < 0 and prev_close <= float(prev[kcm]) and close > float(last[kcm])
        action = self.evaluate_action_from_indicators(df, len(df) - 1, position)

        volume_multiple = volume / vma if vma > 0 else 0.0
        if action == SignalAction.LONG:
            breakout = max((close - float(last[kcu])) / max(atr_value, 1e-9), 0.0)
            stop_loss_estimate = close - atr_value * cfg.atr_stop_multiple if atr_value > 0 else None
        elif action == SignalAction.SHORT:
            breakout = max((float(last[kcl]) - close) / max(atr_value, 1e-9), 0.0)
            stop_loss_estimate = close + atr_value * cfg.atr_stop_multiple if atr_value > 0 else None
        else:
            breakout = 0.0
            stop_loss_estimate = None
        signal_strength = min(1.0, 0.45 + min(breakout, 1.5) * 0.25 + min(volume_multiple / 3, 1) * 0.30)
        if action in {SignalAction.HOLD, SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT}:
            signal_strength = 0.0 if action == SignalAction.HOLD else 0.5

        return StrategySignal(
            symbol=symbol,
            timeframe=timeframe,
            action=action,
            current_price=close,
            suggested_qty=qty,
            signal_strength=signal_strength,
            technical_evidence={
                "signal_candle_time": self._timestamp_to_iso(last.get("timestamp")),
                "signal_candle_close_time": self._timestamp_to_iso(
                    self._timestamp_plus_timeframe(last.get("timestamp"), timeframe)
                ),
                "prev_candle_time": self._timestamp_to_iso(prev.get("timestamp")),
                "prev_candle_close_time": self._timestamp_to_iso(
                    self._timestamp_plus_timeframe(prev.get("timestamp"), timeframe)
                ),
                "close": close,
                "prev_close": prev_close,
                "ema_89": float(last["ema_89"]),
                "kc_upper": float(last[kcu]),
                "prev_kc_upper": float(prev[kcu]),
                "kc_mid": float(last[kcm]),
                "prev_kc_mid": float(prev[kcm]),
                "kc_lower": float(last[kcl]),
                "prev_kc_lower": float(prev[kcl]),
                "volume": volume,
                "vma_20": vma,
                "volume_multiple": volume_multiple,
                "use_ema_filter": cfg.use_ema_filter,
                "use_volume_filter": cfg.use_volume_filter,
                "momentum_filter": cfg.momentum_filter,
                "momentum_long_ok": momentum_long_ok,
                "momentum_short_ok": momentum_short_ok,
                "atr": atr_value,
                "entry_stop_atr": atr_value,
                "atr_stop_multiple": cfg.atr_stop_multiple,
                "position_fraction": cfg.position_fraction,
                "target_leverage": target_leverage,
                "base_nominal": base_nominal,
                "profile_name": cfg.profile_name,
                "strategy_enabled": cfg.enabled,
                "stop_loss_estimate": stop_loss_estimate,
                "breakout_atr": breakout,
                "long_condition": long_condition,
                "short_condition": short_condition,
                "exit_long": exit_long,
                "exit_short": exit_short,
            },
        )

    def _timestamp_to_iso(self, value: Any) -> str | None:
        if value is None:
            return None
        try:
            timestamp = pd.Timestamp(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize("UTC")
            else:
                timestamp = timestamp.tz_convert("UTC")
            return timestamp.isoformat().replace("+00:00", "Z")
        except Exception:  # noqa: BLE001
            return str(value)

    def _timestamp_plus_timeframe(self, value: Any, timeframe: str) -> Any:
        if value is None:
            return None
        try:
            timestamp = pd.Timestamp(value)
            return timestamp + pd.Timedelta(seconds=self._timeframe_seconds(timeframe))
        except Exception:  # noqa: BLE001
            return None

    def _timeframe_seconds(self, timeframe: str) -> int:
        raw = str(timeframe or "1h").strip()
        unit = raw[-1:]
        try:
            value = int(raw[:-1])
        except ValueError:
            return 3600
        if unit == "m":
            return value * 60
        if unit == "h":
            return value * 3600
        if unit == "d":
            return value * 86400
        if unit == "w":
            return value * 7 * 86400
        if unit == "M":
            return value * 30 * 86400
        return 3600

    def _momentum_required_columns(self) -> list[str]:
        cfg = self.config
        if cfg.momentum_filter == "kdj":
            return ["kdj_k", "kdj_d", "kdj_j"]
        return []

    def _momentum_filter_ok(self, row: pd.Series) -> tuple[bool, bool]:
        cfg = self.config
        if cfg.momentum_filter == "kdj":
            k_value = float(row["kdj_k"])
            d_value = float(row["kdj_d"])
            j_value = float(row["kdj_j"])
            return k_value > d_value and j_value >= 50, k_value < d_value and j_value <= 50
        return True, True
