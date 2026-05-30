import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createChart, ColorType, LineStyle, type IChartApi, type IPriceLine, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import type { Candle, DbRow, DenseZonePayload, StrategyProfile } from "./types";

export function MarketChart({
  candles,
  profile,
  orders = [],
  decisions = [],
  denseZone,
  height = 560,
}: {
  candles: Candle[];
  profile?: StrategyProfile;
  orders?: DbRow[];
  decisions?: DbRow[];
  denseZone?: DenseZonePayload;
  height?: number;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const emaRef = useRef<ISeriesApi<"Line"> | null>(null);
  const kcMidRef = useRef<ISeriesApi<"Line"> | null>(null);
  const kcUpperRef = useRef<ISeriesApi<"Line"> | null>(null);
  const kcLowerRef = useRef<ISeriesApi<"Line"> | null>(null);
  const denseLinesRef = useRef<IPriceLine[]>([]);
  const rangeLinesRef = useRef<IPriceLine[]>([]);
  const candlesByTimeRef = useRef<Map<number, Candle>>(new Map());
  const [hoverCandle, setHoverCandle] = useState<Candle | null>(null);
  const [range, setRange] = useState<"7d" | "30d" | "90d" | "all">("90d");
  const [markerDensity, setMarkerDensity] = useState<"compact" | "full">("compact");
  const [showKc, setShowKc] = useState(true);
  const [showEma, setShowEma] = useState(false);
  const [showVolume, setShowVolume] = useState(true);
  const [showOrders, setShowOrders] = useState(true);
  const [showAi, setShowAi] = useState(true);
  const [showDense, setShowDense] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const effectiveHeight = expanded ? Math.max(620, window.innerHeight - 190) : height;
  const focus = hoverCandle || candles.at(-1) || null;
  const prev = useMemo(() => {
    if (!focus) return null;
    const idx = candles.findIndex((item) => item.time === focus.time);
    return idx > 0 ? candles[idx - 1] : null;
  }, [candles, focus]);
  const changePct = focus && prev ? ((focus.close - prev.close) / prev.close) * 100 : 0;
  const visibleCandles = useMemo(() => visibleWindow(candles, range), [candles, range]);
  const visibleStats = useMemo(() => visibleWindowStats(visibleCandles), [visibleCandles]);
  const markerLimit = markerDensity === "compact" ? 60 : 200;
  const orderMarkerCount = useMemo(() => orderMarkers(orders).length, [orders]);
  const aiMarkerCount = useMemo(() => decisionMarkers(decisions).length, [decisions]);

  useEffect(() => {
    if (!rootRef.current) return;
    const chart = createChart(rootRef.current, {
      height: effectiveHeight,
      layout: { background: { type: ColorType.Solid, color: "#07111f" }, textColor: "#94a3b8" },
      grid: { vertLines: { color: "#142033" }, horzLines: { color: "#142033" } },
      rightPriceScale: { borderColor: "#263246", scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: { borderColor: "#263246", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
    });
    const candlesSeries = chart.addCandlestickSeries({
      upColor: "#0a9f5a",
      downColor: "#e11d48",
      borderUpColor: "#0a9f5a",
      borderDownColor: "#e11d48",
      wickUpColor: "#0a9f5a",
      wickDownColor: "#e11d48",
    });
    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "",
      color: "#cbd6e5",
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
    const emaSeries = chart.addLineSeries({ color: "#e5e7eb", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    const kcMidSeries = chart.addLineSeries({ color: "#94a3b8", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const kcUpperSeries = chart.addLineSeries({ color: "#3b82f6", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const kcLowerSeries = chart.addLineSeries({ color: "#3b82f6", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    chart.subscribeCrosshairMove((param) => {
      const timestamp = Number(param.time);
      if (!Number.isFinite(timestamp)) {
        setHoverCandle(null);
        return;
      }
      setHoverCandle(candlesByTimeRef.current.get(timestamp) || null);
    });

    chartRef.current = chart;
    candleRef.current = candlesSeries;
    volumeRef.current = volumeSeries;
    emaRef.current = emaSeries;
    kcMidRef.current = kcMidSeries;
    kcUpperRef.current = kcUpperSeries;
    kcLowerRef.current = kcLowerSeries;
    const resize = () => chart.applyOptions({ width: rootRef.current?.clientWidth || 800 });
    resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      emaRef.current = null;
      kcMidRef.current = null;
      kcUpperRef.current = null;
      kcLowerRef.current = null;
      denseLinesRef.current = [];
      rangeLinesRef.current = [];
    };
  }, [effectiveHeight]);

  useEffect(() => {
    const candleSeries = candleRef.current;
    const volumeSeries = volumeRef.current;
    if (!candleSeries || !volumeSeries) return;
    const candleData = candles.map((item) => ({
      time: Math.floor(new Date(item.time).getTime() / 1000) as UTCTimestamp,
      open: item.open,
      high: item.high,
      low: item.low,
      close: item.close,
    }));
    candlesByTimeRef.current = new Map(candleData.map((item, idx) => [Number(item.time), candles[idx]]));
    const volumeData = candles.map((item) => ({
      time: Math.floor(new Date(item.time).getTime() / 1000) as UTCTimestamp,
      value: item.volume,
      color: item.close >= item.open ? "rgba(10, 159, 90, 0.35)" : "rgba(225, 29, 72, 0.35)",
    }));
    candleSeries.setData(candleData);
    volumeSeries.setData(showVolume ? volumeData : []);
    const params = profile?.params || {};
    const emaLen = Number(params.ema_length || 89);
    const kcLen = Number(params.kc_length || 20);
    const atrLen = Number(params.atr_length || 14);
    const kcMult = Number(params.kc_scalar || 2.8);
    const closes = candles.map((item) => item.close);
    const emaValues = ema(closes, emaLen);
    const kcMid = ema(closes, kcLen);
    const atrValues = atr(candles, atrLen);
    emaRef.current?.setData(showEma ? lineData(candles, emaValues) : []);
    kcMidRef.current?.setData(showKc ? lineData(candles, kcMid) : []);
    kcUpperRef.current?.setData(showKc ? lineData(candles, kcMid.map((value, idx) => value + atrValues[idx] * kcMult)) : []);
    kcLowerRef.current?.setData(showKc ? lineData(candles, kcMid.map((value, idx) => value - atrValues[idx] * kcMult)) : []);
    candleSeries.setMarkers([
      ...(showOrders ? orderMarkers(orders) : []),
      ...(showAi ? decisionMarkers(decisions) : []),
    ].sort((a, b) => Number(a.time) - Number(b.time)).slice(-markerLimit));
    for (const line of denseLinesRef.current) {
      candleSeries.removePriceLine(line);
    }
    for (const line of rangeLinesRef.current) {
      candleSeries.removePriceLine(line);
    }
    denseLinesRef.current = showDense ? denseZoneLines(denseZone).map((item) => candleSeries.createPriceLine(item)) : [];
    rangeLinesRef.current = visibleRangeLines(visibleCandles).map((item) => candleSeries.createPriceLine(item));
    applyVisibleRange(chartRef.current, candleData, range);
  }, [candles, decisions, denseZone, effectiveHeight, markerLimit, orders, profile, range, showAi, showDense, showEma, showKc, showOrders, showVolume, visibleCandles]);

  return (
    <div className={`${expanded ? "fixed inset-4 z-50 flex flex-col" : ""} overflow-hidden rounded-2xl border border-[#263246] bg-[#07111f] shadow-[0_22px_56px_rgba(0,0,0,0.38)]`}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#1e293b] bg-[#0f172a] px-3 py-2 text-xs text-[#cbd5e1]">
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-semibold text-white">专业K线</span>
          <ChartStat label="O" value={focus?.open} />
          <ChartStat label="H" value={focus?.high} tone="good" />
          <ChartStat label="L" value={focus?.low} tone="bad" />
          <ChartStat label="C" value={focus?.close} tone={changePct >= 0 ? "good" : "bad"} />
          <ChartStat label="涨跌" value={`${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%`} tone={changePct >= 0 ? "good" : "bad"} />
          <ChartStat label="量" value={focus?.volume} compact />
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {(["7d", "30d", "90d", "all"] as const).map((item) => (
            <ChartButton key={item} active={range === item} onClick={() => setRange(item)}>{rangeLabel(item)}</ChartButton>
          ))}
          <ChartButton active={showKc} onClick={() => setShowKc((value) => !value)}>KC</ChartButton>
          <ChartButton active={showEma} onClick={() => setShowEma((value) => !value)}>EMA</ChartButton>
          <ChartButton active={showVolume} onClick={() => setShowVolume((value) => !value)}>成交量</ChartButton>
          <ChartButton active={showOrders} onClick={() => setShowOrders((value) => !value)}>订单</ChartButton>
          <ChartButton active={showAi} onClick={() => setShowAi((value) => !value)}>AI</ChartButton>
          <ChartButton active={markerDensity === "full"} onClick={() => setMarkerDensity((value) => value === "compact" ? "full" : "compact")}>
            {markerDensity === "compact" ? "精简标记" : "全部标记"}
          </ChartButton>
          <ChartButton active={showDense} onClick={() => setShowDense((value) => !value)}>密集区</ChartButton>
          <ChartButton active={false} onClick={() => chartRef.current?.timeScale().scrollToRealTime()}>最新</ChartButton>
          <ChartButton active={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? "退出全屏" : "全屏"}</ChartButton>
        </div>
      </div>
      <div className="relative flex-1 bg-[#07111f]">
        {candles.length ? null : (
          <div className="absolute inset-0 z-10 grid place-items-center bg-[#07111f]/85 text-sm text-[#94a3b8]">等待K线数据加载</div>
        )}
        {focus ? <ChartHoverPanel candle={focus} prev={prev} visibleStats={visibleStats} /> : null}
        <div ref={rootRef} style={{ height: effectiveHeight }} className="w-full" />
      </div>
      <div className="grid gap-2 border-t border-[#1f2a3d] bg-[#0b1220] px-3 py-2 text-[11px] text-[#94a3b8] md:grid-cols-3">
        <div>图例：KC 上下轨蓝色，中轨灰色；紫/橙/绿线为密集区，灰色虚线为当前可见区最高/最低。</div>
        <div>当前显示：{rangeLabel(range)} / {visibleCandles.length} 根K线，区间 {formatChartNumber(visibleStats.low)} - {formatChartNumber(visibleStats.high)}。</div>
        <div>标记：订单 {Math.min(orderMarkerCount, markerLimit)}/{orderMarkerCount}，AI {Math.min(aiMarkerCount, markerLimit)}/{aiMarkerCount}。精简模式用于减少图表噪音。</div>
      </div>
    </div>
  );
}

function ChartButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-lg border px-2.5 py-1 text-[11px] transition ${active ? "border-[#60a5fa] bg-[#1d4ed8] text-white shadow-sm" : "border-[#334155] bg-[#111827] text-[#cbd5e1] hover:border-[#64748b]"}`}
    >
      {children}
    </button>
  );
}

function ChartHoverPanel({ candle, prev, visibleStats }: { candle: Candle; prev: Candle | null; visibleStats: VisibleWindowStats }) {
  const change = prev ? ((candle.close - prev.close) / prev.close) * 100 : 0;
  const amplitude = candle.low > 0 ? ((candle.high - candle.low) / candle.low) * 100 : 0;
  return (
    <div className="pointer-events-none absolute left-3 top-3 z-20 w-[258px] rounded-xl border border-[#263246] bg-[#08111f]/92 p-3 text-[11px] text-[#cbd5e1] shadow-[0_18px_44px_rgba(0,0,0,0.45)] backdrop-blur">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="font-semibold text-white">十字光标读数</span>
        <span className="font-mono text-[#94a3b8]">{formatChartTime(candle.time)}</span>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <ChartHoverStat label="开" value={formatChartNumber(candle.open)} />
        <ChartHoverStat label="高" value={formatChartNumber(candle.high)} tone="good" />
        <ChartHoverStat label="低" value={formatChartNumber(candle.low)} tone="bad" />
        <ChartHoverStat label="收" value={formatChartNumber(candle.close)} tone={change >= 0 ? "good" : "bad"} />
        <ChartHoverStat label="涨跌" value={`${change >= 0 ? "+" : ""}${change.toFixed(2)}%`} tone={change >= 0 ? "good" : "bad"} />
        <ChartHoverStat label="振幅" value={`${amplitude.toFixed(2)}%`} />
        <ChartHoverStat label="成交量" value={formatChartNumber(candle.volume, 0)} />
        <ChartHoverStat label="区间高低" value={`${formatChartNumber(visibleStats.low, 0)}-${formatChartNumber(visibleStats.high, 0)}`} />
      </div>
    </div>
  );
}

function ChartHoverStat({ label, value, tone = "default" }: { label: string; value: string; tone?: "default" | "good" | "bad" }) {
  const toneClass = tone === "good" ? "text-[#22c55e]" : tone === "bad" ? "text-[#fb7185]" : "text-[#e5eefb]";
  return (
    <div className="rounded-lg border border-[#1f2a3d] bg-[#0f172a] px-2 py-1.5">
      <div className="text-[10px] text-[#64748b]">{label}</div>
      <div className={`mt-0.5 truncate font-mono font-semibold ${toneClass}`}>{value}</div>
    </div>
  );
}

function ChartStat({ label, value, tone = "default", compact = false }: { label: string; value: unknown; tone?: "default" | "good" | "bad"; compact?: boolean }) {
  const color = tone === "good" ? "text-[#22c55e]" : tone === "bad" ? "text-[#f43f5e]" : "text-[#e2e8f0]";
  const formatted = typeof value === "string" ? value : formatChartNumber(value, compact ? 0 : 2);
  return <span className={`${color} font-mono`}><span className="text-[#94a3b8]">{label}</span> {formatted}</span>;
}

function formatChartNumber(value: unknown, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function rangeLabel(value: "7d" | "30d" | "90d" | "all") {
  return value === "7d" ? "7D" : value === "30d" ? "30D" : value === "90d" ? "90D" : "ALL";
}

type VisibleWindowStats = {
  high: number;
  low: number;
  highTime: string;
  lowTime: string;
};

function visibleWindow(candles: Candle[], range: "7d" | "30d" | "90d" | "all") {
  if (range === "all") return candles;
  const bars = range === "7d" ? 24 * 7 : range === "30d" ? 24 * 30 : 24 * 90;
  return candles.slice(-Math.min(candles.length, bars));
}

function visibleWindowStats(candles: Candle[]): VisibleWindowStats {
  let high = Number.NEGATIVE_INFINITY;
  let low = Number.POSITIVE_INFINITY;
  let highTime = "";
  let lowTime = "";
  for (const candle of candles) {
    if (candle.high > high) {
      high = candle.high;
      highTime = candle.time;
    }
    if (candle.low < low) {
      low = candle.low;
      lowTime = candle.time;
    }
  }
  return {
    high: Number.isFinite(high) ? high : 0,
    low: Number.isFinite(low) ? low : 0,
    highTime,
    lowTime,
  };
}

function visibleRangeLines(candles: Candle[]) {
  const stats = visibleWindowStats(candles);
  if (!stats.high || !stats.low || stats.high === stats.low) return [];
  return [
    {
      price: stats.high,
      color: "#94a3b8",
      lineWidth: 1 as const,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: `区间高 ${formatChartTime(stats.highTime)}`,
    },
    {
      price: stats.low,
      color: "#94a3b8",
      lineWidth: 1 as const,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: `区间低 ${formatChartTime(stats.lowTime)}`,
    },
  ];
}

function formatChartTime(value: string) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:00`;
}

function applyVisibleRange(chart: IChartApi | null, candleData: Array<{ time: UTCTimestamp }>, range: "7d" | "30d" | "90d" | "all") {
  if (!chart || !candleData.length) return;
  if (range === "all") {
    chart.timeScale().fitContent();
    return;
  }
  const bars = range === "7d" ? 24 * 7 : range === "30d" ? 24 * 30 : 24 * 90;
  const visible = candleData.slice(-Math.min(candleData.length, bars));
  const from = visible[0]?.time;
  const to = visible.at(-1)?.time;
  if (from && to) chart.timeScale().setVisibleRange({ from, to });
}

function lineData(candles: Candle[], values: number[]) {
  return candles
    .map((item, idx) => ({
      time: Math.floor(new Date(item.time).getTime() / 1000) as UTCTimestamp,
      value: values[idx],
    }))
    .filter((item) => Number.isFinite(item.value));
}

function ema(values: number[], length: number): number[] {
  const alpha = 2 / (Math.max(1, length) + 1);
  let prev = values[0] || 0;
  return values.map((value, idx) => {
    prev = idx === 0 ? value : value * alpha + prev * (1 - alpha);
    return prev;
  });
}

function atr(candles: Candle[], length: number): number[] {
  const trs = candles.map((item, idx) => {
    const prevClose = idx > 0 ? candles[idx - 1].close : item.close;
    return Math.max(item.high - item.low, Math.abs(item.high - prevClose), Math.abs(item.low - prevClose));
  });
  const alpha = 1 / Math.max(1, length);
  let prev = trs[0] || 0;
  return trs.map((value, idx) => {
    prev = idx === 0 ? value : value * alpha + prev * (1 - alpha);
    return prev;
  });
}

function orderMarkers(orders: DbRow[]) {
  return orders
    .map((row) => {
      const payload = row.payload || {};
      const side = String(payload.side || payload.action || "").toLowerCase();
      if (!side) return null;
      const isBuy = side === "buy" || side.includes("long");
      const isSell = side === "sell" || side.includes("short");
      if (!isBuy && !isSell) return null;
      return {
        time: toTimestamp(row.created_at || String(payload.created_at || "")),
        position: isBuy ? "belowBar" as const : "aboveBar" as const,
        color: isBuy ? "#0a9f5a" : "#e11d48",
        shape: isBuy ? "arrowUp" as const : "arrowDown" as const,
        text: isBuy ? "开仓/买入" : "平仓/卖出",
      };
    })
    .filter((item): item is NonNullable<typeof item> => Boolean(item));
}

function decisionMarkers(decisions: DbRow[]) {
  return decisions
    .map((row) => {
      const payload = row.payload || {};
      const confidence = Number(payload.confidence);
      const action = String(payload.action_suggestion || payload.veto_action || "").toLowerCase();
      const color = action === "block" ? "#b7791f" : action === "reduce" ? "#f59e0b" : "#2454ff";
      return {
        time: toTimestamp(row.created_at),
        position: "inBar" as const,
        color,
        shape: "circle" as const,
        text: Number.isFinite(confidence) ? `AI ${Math.round(confidence * 100)}%` : "AI",
      };
    })
    .filter((item) => Number.isFinite(Number(item.time)));
}

function denseZoneLines(zone?: DenseZonePayload) {
  if (!zone) return [];
  const lines: Array<{ price: number; color: string; title: string; lineWidth?: 1 | 2 | 3 | 4 }> = [];
  const add = (price: unknown, color: string, title: string, lineWidth: 1 | 2 | 3 | 4 = 1) => {
    const value = Number(price);
    if (Number.isFinite(value) && value > 0) lines.push({ price: value, color, title, lineWidth });
  };
  add(zone.zone_high ?? zone.vah, "#b7791f", "密集区上沿", 2);
  add(zone.zone_mid ?? zone.poc, "#2454ff", "POC/中位", 1);
  add(zone.zone_low ?? zone.val, "#0a9f5a", "密集区下沿", 2);
  add(zone.next_zone_low, "#7c3aed", "下一密集区下沿", 1);
  add(zone.previous_zone_high, "#64748b", "前一密集区上沿", 1);
  return lines.map((line) => ({
    price: line.price,
    color: line.color,
    lineWidth: line.lineWidth,
    axisLabelVisible: true,
    title: line.title,
  }));
}

function toTimestamp(value: string) {
  return Math.floor(new Date(value).getTime() / 1000) as UTCTimestamp;
}
