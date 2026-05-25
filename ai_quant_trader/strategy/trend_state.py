from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ai_quant_trader.core.models import Side, utc_now


@dataclass(frozen=True)
class TrendPositionState:
    symbol: str
    side: str
    entry_price: float
    atr_value: float
    atr_stop_multiple: float
    stop_loss_price: float
    opened_at: str
    native_stop_order_id: str | None = None


class TrendStateStore:
    def __init__(self, path: str = "data/state_trend.json") -> None:
        self.path = Path(path)

    def get(self, symbol: str) -> TrendPositionState | None:
        raw = self._load().get(symbol)
        if not isinstance(raw, dict):
            return None
        try:
            return TrendPositionState(
                symbol=str(raw["symbol"]),
                side=str(raw["side"]),
                entry_price=float(raw["entry_price"]),
                atr_value=float(raw["atr_value"]),
                atr_stop_multiple=float(raw["atr_stop_multiple"]),
                stop_loss_price=float(raw["stop_loss_price"]),
                opened_at=str(raw["opened_at"]),
                native_stop_order_id=str(raw["native_stop_order_id"]) if raw.get("native_stop_order_id") else None,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def record_entry(
        self,
        symbol: str,
        side: Side | str,
        entry_price: float,
        atr_value: float,
        atr_stop_multiple: float,
        native_stop_order_id: str | None = None,
    ) -> TrendPositionState:
        normalized_side = str(side.value if isinstance(side, Side) else side)
        if normalized_side == Side.LONG:
            stop_loss_price = entry_price - atr_value * atr_stop_multiple
        elif normalized_side == Side.SHORT:
            stop_loss_price = entry_price + atr_value * atr_stop_multiple
        else:
            raise ValueError(f"unsupported trend state side: {side}")
        state = TrendPositionState(
            symbol=symbol,
            side=normalized_side,
            entry_price=float(entry_price),
            atr_value=float(atr_value),
            atr_stop_multiple=float(atr_stop_multiple),
            stop_loss_price=float(stop_loss_price),
            opened_at=utc_now().isoformat(),
            native_stop_order_id=native_stop_order_id,
        )
        data = self._load()
        data[symbol] = asdict(state)
        self._save(data)
        return state

    def set_native_stop_order_id(self, symbol: str, order_id: str | None) -> TrendPositionState | None:
        state = self.get(symbol)
        if state is None:
            return None
        updated = TrendPositionState(
            symbol=state.symbol,
            side=state.side,
            entry_price=state.entry_price,
            atr_value=state.atr_value,
            atr_stop_multiple=state.atr_stop_multiple,
            stop_loss_price=state.stop_loss_price,
            opened_at=state.opened_at,
            native_stop_order_id=order_id,
        )
        data = self._load()
        data[symbol] = asdict(updated)
        self._save(data)
        return updated

    def clear(self, symbol: str) -> None:
        data = self._load()
        if symbol in data:
            data.pop(symbol, None)
            self._save(data)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.path)
