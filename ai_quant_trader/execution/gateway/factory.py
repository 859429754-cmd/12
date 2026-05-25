from __future__ import annotations

from typing import Any, Literal

from ai_quant_trader.execution.gateway.base import BaseExchangeGateway
from ai_quant_trader.execution.gateway.gate_real import GateRealGateway
from ai_quant_trader.execution.gateway.mock import MockExchangeGateway

ExecutionMode = Literal["mock", "live"]


def execution_mode_from_config(config: Any) -> ExecutionMode:
    runtime = getattr(config, "runtime", config)
    mode = getattr(runtime, "execution_mode", None)
    if mode in {"mock", "live"}:
        return mode
    return "mock" if getattr(runtime, "dry_run", True) else "live"


def create_exchange_gateway(config_or_mode: Any, account_slot: str = "default") -> BaseExchangeGateway:
    mode = config_or_mode if isinstance(config_or_mode, str) else execution_mode_from_config(config_or_mode)
    if mode == "live":
        return GateRealGateway(account_slot=account_slot)
    if mode == "mock":
        return MockExchangeGateway()
    raise ValueError(f"unsupported_execution_mode:{mode}")
