from __future__ import annotations

import os

from ai_quant_trader.execution.gateio import GateExecutionClient

GATE_ACCOUNT_ENV = {
    "default": ("GATEIO_API_KEY", "GATEIO_API_SECRET"),
    "trend": ("GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"),
    "range": ("GATEIO_RANGE_API_KEY", "GATEIO_RANGE_API_SECRET"),
}


class GateRealGateway(GateExecutionClient):
    """Gate.io 真实交易网关。

    真实网关固定以 dry_run=False 初始化。控制台从模拟切换到真实运行必须先通过 Trade PIN，
    然后由工厂层实例化这个网关，业务层不允许绕过工厂直接构造真实网关。
    """

    mode = "live"

    def __init__(self, account_slot: str = "default") -> None:
        if account_slot not in GATE_ACCOUNT_ENV:
            raise ValueError(f"unsupported_gate_account_slot:{account_slot}")
        api_key_env, api_secret_env = GATE_ACCOUNT_ENV[account_slot]
        if account_slot == "trend" and not _env_pair_configured(api_key_env, api_secret_env):
            api_key_env, api_secret_env = GATE_ACCOUNT_ENV["default"]
        super().__init__(
            dry_run=False,
            api_key_env=api_key_env,
            api_secret_env=api_secret_env,
            account_slot=account_slot,
        )

    async def contract_size(self, symbol: str) -> float:
        return await self._contract_size(symbol)


def _env_pair_configured(api_key_env: str, api_secret_env: str) -> bool:
    return bool(os.getenv(api_key_env, "").strip()) and bool(os.getenv(api_secret_env, "").strip())
