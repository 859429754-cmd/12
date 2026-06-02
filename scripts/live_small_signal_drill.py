from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_quant_trader.brain.deepseek import DeepSeekBrain
from ai_quant_trader.core.config import load_config
from ai_quant_trader.core.control import RuntimeControlManager
from ai_quant_trader.core.models import NewsDigest, OrderRequest, PositionSnapshot, Side, SignalAction, StrategySignal, VetoAction
from ai_quant_trader.data.market import MarketDataClient
from ai_quant_trader.data.orderflow import MultiExchangeOrderflowClient
from ai_quant_trader.execution.gateway import create_exchange_gateway
from ai_quant_trader.execution.lifecycle import OrderLifecycleManager, OrderRejected, OrderSubmissionUncertain
from ai_quant_trader.features.dense_zone import DenseZoneAnalyzer
from ai_quant_trader.features.orderflow import OrderflowAggregator
from ai_quant_trader.features.patterns import PatternDetector
from ai_quant_trader.features.regime import RegimePatternAnalyzer
from ai_quant_trader.risk.manager import RiskManager
from ai_quant_trader.storage.sqlite import SQLiteStore
from ai_quant_trader.strategy.indicators import atr


def _load_runtime_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


async def run_drill(args: argparse.Namespace) -> dict[str, Any]:
    if not args.execute_live:
        raise RuntimeError("Refusing to run live drill without --execute-live.")
    _load_runtime_env(Path(args.env_file))
    config = load_config(args.config)
    symbol = args.symbol
    side: Literal["long", "short"] = args.side
    action = SignalAction.LONG if side == "long" else SignalAction.SHORT
    close_side = "sell" if side == "long" else "buy"

    market = MarketDataClient()
    orderflow_client = MultiExchangeOrderflowClient(config.orderflow.exchanges, live_public_data=True)
    gateway = create_exchange_gateway("live", account_slot="trend")
    store = SQLiteStore(config.runtime.database_path, config.runtime.audit_log_path)
    lifecycle = OrderLifecycleManager(store, gateway_mode="live")
    runtime_state = RuntimeControlManager(store, config_path=args.config).load_state([symbol])
    risk_manager = RiskManager(config.risk, runtime_state)
    try:
        candles = await market.fetch_ohlcv(symbol, args.timeframe, limit=max(args.limit, 240), source=args.source)
        current_price = float(candles["close"].iloc[-1])
        atr_series = atr(candles, config.strategy.trend.atr_length)
        atr_value = float(atr_series.dropna().iloc[-1])
        stop_multiple = float(config.strategy.trend.atr_stop_multiple)
        stop_price = current_price - atr_value * stop_multiple if side == "long" else current_price + atr_value * stop_multiple

        positions = await gateway.fetch_positions([symbol])
        position = positions[0] if positions else PositionSnapshot(symbol=symbol, side=Side.FLAT)
        if position.side != Side.FLAT or abs(position.qty) > 0:
            raise RuntimeError(f"Refusing live drill: existing position detected for {symbol}.")

        balance = await gateway.fetch_balance_summary()
        equity = float(balance.get("usdt_total") or balance.get("total_equity") or balance.get("usdt_free") or 0.0)
        if equity <= 0:
            raise RuntimeError("Refusing live drill: unable to read positive live equity.")

        min_contracts = await gateway.minimum_order_amount(symbol, current_price)
        contract_size = await gateway.contract_size(symbol)
        base_amount = float(min_contracts) * float(contract_size)
        notional = base_amount * current_price
        if notional > args.max_notional_usdt:
            raise RuntimeError(f"Minimum order notional {notional:.2f} exceeds cap {args.max_notional_usdt:.2f}.")

        orderflow_rows = await orderflow_client.fetch_summaries(symbol)
        orderflow = OrderflowAggregator(config.orderflow.weights).aggregate(symbol, orderflow_rows)
        dense_zone = DenseZoneAnalyzer().calculate(symbol, candles)
        pattern = PatternDetector().detect(symbol, candles)
        regime_pattern = RegimePatternAnalyzer().analyze(symbol, candles, dense_zone, pattern)
        news_digest = _latest_cached_news(store)

        signal = StrategySignal(
            symbol=symbol,
            timeframe=args.timeframe,
            action=action,
            current_price=current_price,
            suggested_qty=base_amount,
            signal_strength=args.signal_strength,
            technical_evidence={
                "drill": True,
                "entry_stop_atr": atr_value,
                "atr": atr_value,
                "atr_stop_multiple": stop_multiple,
                "source": "manual_live_small_signal_drill",
                "regime_candidate": regime_pattern.regime_candidate,
                "strategy_allowed": regime_pattern.strategy_allowed,
            },
        )
        signal = RegimePatternAnalyzer().enrich_signal(signal, regime_pattern)
        brain = DeepSeekBrain(base_url=config.ai.base_url, model=args.model or config.ai.emergency_screening_model)
        ai = await brain.analyze_symbol(signal, orderflow, dense_zone, pattern, news_digest, regime_pattern)
        store.insert("ai_decisions", ai, symbol)

        if ai.veto_action == VetoAction.BLOCK and not args.allow_ai_block_override:
            raise RuntimeError("DeepSeek blocked the fake signal. Re-run with --allow-ai-block-override for execution-path drill.")

        risk = risk_manager.evaluate(signal, ai, equity, positions)
        if risk.allowed and 0 < risk.position_scale < 1 and risk.clipped_qty < base_amount:
            adjusted_target_qty = base_amount / risk.position_scale
            signal = signal.model_copy(
                update={
                    "suggested_qty": adjusted_target_qty,
                    "technical_evidence": {
                        **signal.technical_evidence,
                        "minimum_live_drill_target_adjusted": True,
                        "risk_position_tier": risk.position_tier,
                    },
                }
            )
            risk = risk_manager.evaluate(signal, ai, equity, positions)
        if not risk.allowed:
            raise RuntimeError(f"RiskManager blocked the live drill: {risk.reason}.")
        if risk.clipped_qty + 1e-12 < base_amount:
            raise RuntimeError(
                f"RiskManager clipped qty {risk.clipped_qty:.8f} below exchange minimum {base_amount:.8f}."
            )

        entry_request = OrderRequest(
            symbol=symbol,
            side="buy" if side == "long" else "sell",
            amount=base_amount,
            reduce_only=False,
            client_order_id=f"aiq_drill_{uuid.uuid4().hex[:17]}",
            reason=f"manual_live_drill_ai_{ai.veto_action}",
        )
        entry_order = await lifecycle.submit_market_order(gateway, entry_request)
        store.insert("orders", entry_order, symbol)
        stop_order_id: str | None = None
        try:
            stop_request = OrderRequest(
                symbol=symbol,
                side=close_side,
                amount=base_amount,
                reduce_only=True,
                client_order_id=f"aiq_dstop_{uuid.uuid4().hex[:16]}",
                reason="manual_live_drill_native_stop",
            )
            stop_order = await lifecycle.submit_stop_loss_order(
                gateway,
                stop_request,
                stop_price,
            )
            stop_order_id = stop_order.exchange_order_id
            store.insert("orders", stop_order, symbol)
            await asyncio.sleep(max(float(args.hold_seconds), 0.0))
            close_order = await lifecycle.close_position(gateway, symbol, reason="manual_live_drill_close")
            if close_order:
                store.insert("orders", close_order, symbol)
        except (OrderRejected, OrderSubmissionUncertain) as exc:
            raise RuntimeError("native_stop_submit_failed_manual_gate_required") from exc
        except Exception:
            close_order = await lifecycle.close_position(gateway, symbol, reason="manual_live_drill_emergency_close")
            if close_order:
                store.insert("orders", close_order, symbol)
            raise
        finally:
            if stop_order_id:
                await lifecycle.cancel_order(
                    gateway,
                    symbol=symbol,
                    order_id=stop_order_id,
                    client_order_id=f"aiq_dcancel_{uuid.uuid4().hex[:13]}",
                    trigger=True,
                    gateway_mode="live",
                )

        await asyncio.sleep(2.0)
        final_position = (await gateway.fetch_positions([symbol]))[0]
        if final_position.side != Side.FLAT or abs(final_position.qty) > 0:
            raise RuntimeError(f"Live drill did not end flat: {final_position.model_dump(mode='json')}")

        return {
            "ok": True,
            "symbol": symbol,
            "side": side,
            "model": brain.model,
            "entry_order_id": entry_order.exchange_order_id,
            "native_stop_order_id": stop_order_id,
            "closed_flat": True,
            "base_amount": base_amount,
            "estimated_notional_usdt": notional,
            "stop_price": stop_price,
            "ai": {
                "regime": ai.regime,
                "direction": ai.direction,
                "confidence": ai.confidence,
                "veto_action": ai.veto_action,
                "action_suggestion": ai.action_suggestion,
                "brief_reason": ai.brief_reason,
            },
            "risk": {
                "allowed": risk.allowed,
                "reason": risk.reason,
                "position_tier": risk.position_tier,
                "position_scale": risk.position_scale,
                "decision_score": risk.decision_score,
                "target_notional": risk.target_notional,
                "clipped_qty": risk.clipped_qty,
                "warnings": risk.warnings[:5],
            },
            "inputs": {
                "orderflow_alignment": orderflow.alignment_hint,
                "orderflow_quality": orderflow.data_quality,
                "dense_zone_position": dense_zone.current_position,
                "dense_zone_low": dense_zone.zone_low,
                "dense_zone_high": dense_zone.zone_high,
                "pattern": pattern.pattern_type,
                "news_items": len(news_digest.items),
                "news_warnings": news_digest.warnings[:5],
            },
        }
    finally:
        await market.close()
        await orderflow_client.close()
        await gateway.close()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a minimal live Gate.io signal drill with native stop and immediate close.")
    parser.add_argument("--execute-live", action="store_true", help="Required. Places and closes a real minimum-size live order.")
    parser.add_argument("--allow-ai-block-override", action="store_true", help="Allow the drill to continue when AI vetoes the fake signal.")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--env-file", default=".env.runtime")
    parser.add_argument("--symbol", default="ETH/USDT:USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--source", default="binance")
    parser.add_argument("--side", choices=["long", "short"], default="long")
    parser.add_argument("--model", default=None)
    parser.add_argument("--limit", type=int, default=360)
    parser.add_argument("--signal-strength", type=float, default=0.75)
    parser.add_argument("--max-notional-usdt", type=float, default=50.0)
    parser.add_argument("--hold-seconds", type=float, default=5.0)
    args = parser.parse_args()
    result = asyncio.run(run_drill(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _latest_cached_news(store: SQLiteStore) -> NewsDigest:
    rows = store.fetch_payloads("news_summaries", limit=1)
    if rows:
        try:
            return NewsDigest.model_validate(rows[0].get("payload") or {})
        except Exception:
            pass
    return NewsDigest(
        summary="No cached news digest available for live drill; AI receives technical, orderflow, dense-zone, and pattern inputs only.",
        warnings=["live_drill_news_cache_empty"],
    )


if __name__ == "__main__":
    main()
