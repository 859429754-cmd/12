# Build Walk-Forward Optimization Harness

Status: ready-for-agent
Labels: ready-for-agent

## Problem

No system can guarantee positive optimization in live markets. The correct target is evidence that a change improves risk-adjusted performance out-of-sample after fees, slippage, and realistic execution assumptions.

## Requirements

- Split historical data into training, validation, and out-of-sample windows.
- Run rolling or walk-forward evaluation.
- Compare:
  - baseline trend strategy
  - AI veto overlay
  - AI reduce overlay
  - AI full-size strict consensus overlay
- Report:
  - return
  - max drawdown
  - win rate
  - profit factor
  - trade count
  - cost ratio
  - max adverse excursion
  - parameter stability
- Reject changes that only improve in-sample results or materially worsen drawdown.

## Acceptance Criteria

- Harness runs without calling DeepSeek per candle.
- Results include clear baseline vs candidate comparison.
- Negative optimization triggers automatic rejection/revert recommendation.
- Tests cover scoring and rejection rules.

