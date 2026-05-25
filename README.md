# AI Quant Trader

AI-driven crypto trading system for Gate.io USDT perpetual execution.

## Current Scope

- Execution: Gate.io through the gateway layer.
- Market data: Binance, OKX, Gate.io, CryptoCompare fallback paths.
- AI: DeepSeek structured decisions with local hard risk controls.
- Console: FastAPI + React workbench with QuantDinger-style platform shell.
- Storage: SQLite WAL plus JSONL audit log.
- Notifications: external push channels are not part of the runtime.

## Safety Defaults

- Cold start keeps opening paused unless explicitly authorized.
- Live execution must pass gateway isolation, symbol authorization, local technical signal checks, AI veto rules, and total leverage limits.
- AI cannot bypass hard risk controls or place live orders directly.
- `.env.runtime` contains live secrets and must never be logged, printed, or committed.

## Run

```bash
python -m pip install -r requirements.txt
python main.py --once
```

Console:

```bash
uvicorn ai_quant_trader.api.server:app --host 127.0.0.1 --port 8090
```

Frontend build:

```bash
cd console
npm.cmd run build
```

## Verify

```bash
python -m compileall ai_quant_trader tests scripts
python -m pytest -q
cd console && npm.cmd run build
```
