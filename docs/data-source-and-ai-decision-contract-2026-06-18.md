# 数据源与 AI 决策合同（2026-06-18）

以后以本文件为准，忽略之前“Gate.io 行情源优先参与市场判断”的方案。

## 1. 数据源优先级

Gate.io 仍然是当前实盘执行交易所、账户对账来源、持仓来源和订单生命周期来源。

市场行情、K 线、订单流和 BTC/ETH 风向标优先采用全球主流高流动性交易所数据：

```text
K线 / ticker 自动行情源：Binance -> OKX -> Bybit -> Gate.io
订单流自动采集源：Binance -> OKX -> Bybit
```

工程理由：

- Binance 是第一行情源。
- OKX 和 Bybit 是主要备选源。
- Gate.io 作为执行交易所和最后行情兜底，不再作为市场判断的优先行情源。
- 数据源失败必须降低数据健康状态；不能用合成数据冒充可实盘依据。

实现锚点：

- `ai_quant_trader/data/market.py::AUTO_MARKET_SOURCES`
- `ai_quant_trader/data/orderflow.py::MultiExchangeOrderflowClient`
- `config/config.yaml::orderflow.exchanges`
- `tests/test_market_sources.py`

## 2. AI 决策是否删除旧订单流

新版 AI 决策不删除旧版订单流。

AI / RiskManager 仍然使用：

- `orderflow_alignment`
- `orderflow_confirmation_score`
- `source_count`
- `data_quality`
- 主动买卖、盘口不平衡、大单事件等订单流聚合信息

新增的新闻背景、BTC/ETH 风向标、ETH 相对 BTC 轮动评分，只是补充决策输入，不允许覆盖订单流，也不允许绕过本地策略信号。

当前生产链路：

```text
ETH 1h 本地趋势信号
  -> K线 / 形态 / 密集区 / 订单流 / 新闻背景 / BTC-ETH风向标
  -> DeepSeek 结构化 JSON 评估一次
  -> RiskManager 五档仓位裁剪
  -> 账户1执行
  -> 账户2按账户1决策镜像执行
```

## 3. Walk-forward 自动学习边界

Walk-forward 可以实现，但第一版必须是离线研究与提案系统，不能直接自动改实盘参数。

生产级安全边界：

- 历史数据分段：train / validation / out-of-sample。
- 输出参数候选、收益、最大回撤、PF、胜率、交易次数、资金费、滑点、手续费、AI overlay 前后差异。
- 如果 AI overlay 或新参数在样本外变差，必须标记为 `rejected`。
- 新参数必须生成 admin 提案，不能自动热更新到实盘。
- 任何自动学习结果必须版本化、可回滚、可复现。

允许的后续升级：

- 离线 walk-forward 每日或每周生成候选参数。
- 控制台展示候选方案和样本外结果。
- 管理员手动批准后进入小仓/模拟观察。
- 观察期通过后再允许进入实盘配置。

禁止：

- 模型根据最近几笔交易自动追涨杀跌改参数。
- AI 直接修改实盘参数。
- 没有样本外验证就自动放大仓位。
