from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length, min_periods=length).mean()


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def keltner_channel(df: pd.DataFrame, length: int, scalar: float, atr_length: int | None = None) -> pd.DataFrame:
    middle = ema(df["close"], length)
    atr_value = atr(df, atr_length or length)
    return pd.DataFrame(
        {
            f"KCMe_{length}_{scalar}": middle,
            f"KCUe_{length}_{scalar}": middle + scalar * atr_value,
            f"KCLe_{length}_{scalar}": middle - scalar * atr_value,
        },
        index=df.index,
    )


def kdj(df: pd.DataFrame, length: int = 9, k_smooth: int = 3, d_smooth: int = 3) -> pd.DataFrame:
    low_min = df["low"].rolling(length, min_periods=length).min()
    high_max = df["high"].rolling(length, min_periods=length).max()
    rsv = ((df["close"] - low_min) / (high_max - low_min).replace(0, pd.NA) * 100).fillna(50.0)
    k = rsv.ewm(alpha=1 / k_smooth, adjust=False, min_periods=k_smooth).mean()
    d = k.ewm(alpha=1 / d_smooth, adjust=False, min_periods=d_smooth).mean()
    j = 3 * k - 2 * d
    return pd.DataFrame({"kdj_k": k, "kdj_d": d, "kdj_j": j}, index=df.index)
