# 2026-06-26 Historical Orderflow Proxy Backfill

## Purpose

This research step starts validating whether orderflow has statistical value for the ETH 1h trend strategy.

It does not change live trading logic. It does not call DeepSeek. It does not send orders.

## Current Contract

- Strategy version: `KC(20,2.8) + VOL(20,2.5) + KDJ(9,3,3) + ATR stop 1.5`
- Current TV-aligned trade count: `291`
- Source strategy research file: `data/research/pure_strategy_tier_research_eth_2022_2026_no_ema.json`
- Historical news and full historical order book depth remain unavailable.

## Implementation

New script:

```powershell
python scripts\historical_orderflow_backfill.py
```

The script reads the pure strategy `features` array and, for each entry, reconstructs a Binance futures `aggTrades` orderflow proxy before `entry_time`.

Default windows:

```text
60 minutes
240 minutes
```

Computed fields include:

- taker buy quote
- taker sell quote
- CVD quote
- direction-adjusted CVD ratio
- large trade count
- large trade quote
- direction-adjusted large trade ratio
- max trade notional
- alignment: `aligned / neutral / conflict`

## Lookahead Guard

Each orderflow window ends at `entry_time` exclusive.

If any aggregate trade timestamp is greater than or equal to `entry_time`, the script raises an error. Missing daily archives are marked as `missing`, not interpreted as bullish or bearish evidence.

## Smoke Result

Command:

```powershell
python scripts\historical_orderflow_backfill.py --input data\research\pure_strategy_tier_research_eth_2022_2026_no_ema.json --output data\research\historical_orderflow_proxy_eth_sample10.json --windows 60,240 --max-features 10 --download
```

Result:

```text
60m: 10 usable / 10 total
240m: 10 usable / 10 total
```

Early sample observation:

- `directional_cvd_quote_ratio` shows positive separation in the first 10 trades.
- Sample size is far too small to change live RiskManager weights.
- Downloaded 10 daily Binance `aggTrades` archives use about 224 MB locally.

## Full 291-Trade Result

Command:

```powershell
python scripts\historical_orderflow_backfill.py --input data\research\pure_strategy_tier_research_eth_2022_2026_no_ema.json --output data\research\historical_orderflow_proxy_eth_2022_2026.json --windows 60,240 --download --progress-every 25 --checkpoint-every 10
```

Result:

```text
60m: 291 usable / 291 total
240m: 291 usable / 291 total
```

Cache footprint:

```text
312 daily Binance aggTrades archives
~6.7 GB
```

Top 60m effects:

```text
trade_count         effect 0.390
total_quote         effect 0.353
taker_buy_quote     effect 0.351
taker_sell_quote    effect 0.347
large_trade_quote   effect 0.293
large_trade_count   effect 0.285
```

Top 240m effects:

```text
trade_count         effect 0.285
taker_sell_quote    effect 0.278
total_quote         effect 0.273
taker_buy_quote     effect 0.264
large_trade_quote   effect 0.252
large_trade_count   effect 0.242
```

Important interpretation:

- Historical orderflow proxy has stronger evidence as a **participation / liquidity / impulse quality factor** than as a directional factor.
- Directional CVD is weak in this sample.
- `aligned` orderflow has larger average PnL than `neutral`, but does not improve win rate by itself.
- Therefore orderflow should not be used as "same direction means full size". It should first be used as a quality confirmation and risk cap modifier.

## Validation

```powershell
python -m pytest tests\test_historical_orderflow_backfill.py tests\test_pure_strategy_tier_research.py -q
# 9 passed

python -m compileall ai_quant_trader tests scripts
# passed

python -m pytest -q
# 280 passed

python scripts\public_repo_preflight.py
# ok=true
```

## Limits

- Binance `aggTrades` is only an orderflow proxy.
- It does not reconstruct full historical order book depth.
- It does not validate historical news.
- Full 291-trade backfill may require several GB of local data.
- Results must go through walk-forward validation before live sizing changes.

## Next Step

Run walk-forward tests on orderflow-aware sizing candidates. The first candidate should increase size only when participation/liquidity quality is high, not merely when CVD points in the strategy direction.
