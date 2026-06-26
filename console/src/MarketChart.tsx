import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  createChart,
  ColorType,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Candle, DbRow, DenseZonePayload, StrategyProfile } from "./types";

type ChartRange = "1d" | "7d" | "30d" | "90d" | "all";
type ChartLayer = "kc" | "volume" | "orders" | "ai" | "dense" | "levels";

const RANGE_OPTIONS: ChartRange[] = ["1d", "7d", "30d", "90d", "all"];

export function MarketChart({
  candles,
  profile,
  orders = [],
  decisions = [],
  denseZone,
  height = 560,
  timeframe = "1h",
  timeframeOptions = [],
  onTimeframeChange,
}: {
  candles: Candle[];
  profile?: StrategyProfile;
  orders?: DbRow[];
  decisions?: DbRow[];
  denseZone?: DenseZonePayload;
  height?: number;
  timeframe?: string;
  timeframeOptions?: string[];
  onTimeframeChange?: (value: string) => void;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const kcMidRef = useRef<ISeriesApi<"Line"> | null>(null);
  const kcUpperRef = useRef<ISeriesApi<"Line"> | null>(null);
  const kcLowerRef = useRef<ISeriesApi<"Line"> | null>(null);
  const denseLinesRef = useRef<IPriceLine[]>([]);
  const levelLinesRef = useRef<IPriceLine[]>([]);
  const candlesByTimeRef = useRef<Map<number, Candle>>(new Map());
  const [hoverCandle, setHoverCandle] = useState<Candle | null>(null);
  const [range, setRange] = useState<ChartRange>("30d");
  const [rangeResetToken, setRangeResetToken] = useState(0);
  const [markerDensity, setMarkerDensity] = useState<"compact" | "full">("compact");
  const [expanded, setExpanded] = useState(false);
  const [layers, setLayers] = useState<Record<ChartLayer, boolean>>({
    kc: true,
    volume: true,
    orders: true,
    ai: false,
    dense: false,
    levels: false,
  });
  const [isMobile, setIsMobile] = useState(() => (typeof window === "undefined" ? false : window.innerWidth < 720));

  useEffect(() => {
    const update = () => setIsMobile(window.innerWidth < 720);
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  const effectiveHeight = expanded
    ? Math.max(620, (typeof window === "undefined" ? 780 : window.innerHeight) - 132)
    : isMobile
      ? Math.min(Math.max(height, 460), 560)
      : height;
  const chartCandles = useMemo(() => sanitizeCandles(candles), [candles]);
  const focus = hoverCandle || chartCandles.at(-1) || null;
  const prev = useMemo(() => {
    if (!focus) return null;
    const idx = chartCandles.findIndex((item) => item.time === focus.time);
    return idx > 0 ? chartCandles[idx - 1] : null;
  }, [chartCandles, focus]);
  const changePct = focus && prev ? ((focus.close - prev.close) / prev.close) * 100 : 0;
  const visibleCandles = useMemo(() => visibleWindow(chartCandles, range, timeframe), [chartCandles, range, timeframe]);
  const visibleStats = useMemo(() => visibleWindowStats(visibleCandles), [visibleCandles]);
  const markerLimit = markerDensity === "compact" ? 45 : 180;
  const allOrderMarkers = useMemo(() => orderMarkers(orders), [orders]);
  const allDecisionMarkers = useMemo(() => decisionMarkers(decisions), [decisions]);
  const candleData = useMemo(() => chartCandles.map(toCandlePoint), [chartCandles]);
  const latestPrice = chartCandles.at(-1)?.close;

  useEffect(() => {
    if (!rootRef.current) return;
    const chart = createChart(rootRef.current, {
      height: effectiveHeight,
      layout: {
        background: { type: ColorType.Solid, color: "#050b14" },
        textColor: "#9fb0c6",
        fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
      },
      grid: {
        vertLines: { color: "rgba(51, 65, 85, 0.18)" },
        horzLines: { color: "rgba(51, 65, 85, 0.22)" },
      },
      rightPriceScale: {
        borderColor: "#1f2a3d",
        entireTextOnly: true,
        scaleMargins: { top: 0.08, bottom: 0.2 },
      },
      timeScale: {
        borderColor: "#1f2a3d",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 12,
        barSpacing: isMobile ? 5 : 7,
        minBarSpacing: 1.5,
        fixLeftEdge: false,
        fixRightEdge: false,
        lockVisibleTimeRangeOnResize: true,
        rightBarStaysOnScroll: true,
      },
      crosshair: {
        mode: 0,
        vertLine: { color: "#64748b", width: 1, style: LineStyle.Dashed, labelBackgroundColor: "#1d4ed8" },
        horzLine: { color: "#64748b", width: 1, style: LineStyle.Dashed, labelBackgroundColor: "#1d4ed8" },
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
      },
      localization: {
        priceFormatter: (price: number) => formatChartNumber(price, price >= 100 ? 2 : 4),
        timeFormatter: (time: UTCTimestamp) => formatChartTimeFromSeconds(Number(time)),
      },
    });

    const candlesSeries = chart.addCandlestickSeries({
      upColor: "#22c55e",
      downColor: "#f43f5e",
      borderUpColor: "#22c55e",
      borderDownColor: "#f43f5e",
      wickUpColor: "#22c55e",
      wickDownColor: "#f43f5e",
      priceLineColor: "#60a5fa",
      lastValueVisible: true,
      priceLineVisible: true,
    });
    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "",
      color: "#64748b",
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const kcMidSeries = chart.addLineSeries({ color: "#7c8ca3", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const kcUpperSeries = chart.addLineSeries({ color: "#3b82f6", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const kcLowerSeries = chart.addLineSeries({ color: "#3b82f6", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });

    chart.subscribeCrosshairMove((param) => {
      const timestamp = Number(param.time);
      setHoverCandle(Number.isFinite(timestamp) ? candlesByTimeRef.current.get(timestamp) || null : null);
    });

    chartRef.current = chart;
    candleRef.current = candlesSeries;
    volumeRef.current = volumeSeries;
    kcMidRef.current = kcMidSeries;
    kcUpperRef.current = kcUpperSeries;
    kcLowerRef.current = kcLowerSeries;

    const resize = () => {
      chart.applyOptions({ width: rootRef.current?.clientWidth || 800, height: effectiveHeight });
    };
    resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      kcMidRef.current = null;
      kcUpperRef.current = null;
      kcLowerRef.current = null;
      denseLinesRef.current = [];
      levelLinesRef.current = [];
    };
  }, [effectiveHeight, isMobile]);

  useEffect(() => {
    chartRef.current?.applyOptions({
      rightPriceScale: { scaleMargins: { top: 0.08, bottom: layers.volume ? 0.2 : 0.08 } },
    });
  }, [layers.volume]);

  useEffect(() => {
    const candleSeries = candleRef.current;
    const volumeSeries = volumeRef.current;
    if (!candleSeries || !volumeSeries) return;
    candlesByTimeRef.current = new Map(candleData.map((item, idx) => [Number(item.time), chartCandles[idx]]));
    candleSeries.setData(candleData);
    volumeSeries.setData(layers.volume ? chartCandles.map(toVolumePoint) : []);
  }, [candleData, chartCandles, layers.volume]);

  useEffect(() => {
    const params = profile?.params || {};
    const kcLen = readNumericParam(params, ["kc_length", "kc_len"], 20);
    const atrLen = readNumericParam(params, ["atr_length", "kc_atr_len", "atr_len"], 14);
    const kcMult = readNumericParam(params, ["kc_scalar", "kc_mult"], 2.8);
    const closes = chartCandles.map((item) => item.close);
    const kcMid = ema(closes, kcLen);
    const atrValues = atr(chartCandles, atrLen);
    kcMidRef.current?.setData(layers.kc ? lineData(chartCandles, kcMid) : []);
    kcUpperRef.current?.setData(layers.kc ? lineData(chartCandles, kcMid.map((value, idx) => value + atrValues[idx] * kcMult)) : []);
    kcLowerRef.current?.setData(layers.kc ? lineData(chartCandles, kcMid.map((value, idx) => value - atrValues[idx] * kcMult)) : []);
  }, [chartCandles, layers.kc, profile]);

  useEffect(() => {
    const candleSeries = candleRef.current;
    if (!candleSeries) return;
    candleSeries.setMarkers(
      [
        ...(layers.orders ? allOrderMarkers : []),
        ...(layers.ai ? allDecisionMarkers : []),
      ]
        .sort((a, b) => Number(a.time) - Number(b.time))
        .slice(-markerLimit),
    );
  }, [allDecisionMarkers, allOrderMarkers, layers.ai, layers.orders, markerLimit]);

  useEffect(() => {
    const candleSeries = candleRef.current;
    if (!candleSeries) return;
    for (const line of denseLinesRef.current) candleSeries.removePriceLine(line);
    for (const line of levelLinesRef.current) candleSeries.removePriceLine(line);
    denseLinesRef.current = layers.dense ? denseZoneLines(denseZone).map((item) => candleSeries.createPriceLine(item)) : [];
    levelLinesRef.current = layers.levels ? visibleRangeLines(visibleCandles).map((item) => candleSeries.createPriceLine(item)) : [];
  }, [denseZone, layers.dense, layers.levels, visibleCandles]);

  useEffect(() => {
    applyVisibleRange(chartRef.current, candleData, range, timeframe);
  }, [candleData.length, range, rangeResetToken, timeframe]);

  const toggleLayer = (layer: ChartLayer) => setLayers((value) => ({ ...value, [layer]: !value[layer] }));
  const resetRange = () => setRangeResetToken((value) => value + 1);

  return (
    <div className={`${expanded ? "fixed inset-2 z-50 flex flex-col sm:inset-4" : ""} overflow-hidden rounded-2xl border border-[#1f2a3d] bg-[#050b14] shadow-[0_22px_56px_rgba(0,0,0,0.46)]`}>
      <div className="flex flex-col gap-3 border-b border-[#1e293b] bg-[#07111f] px-3 py-3 text-xs text-[#cbd5e1]">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <span className="flex-none whitespace-nowrap text-sm font-semibold text-white">专业 K 线</span>
            <span className={`${changePct >= 0 ? "text-[#22c55e]" : "text-[#f43f5e]"} font-mono font-semibold`}>
              {changePct >= 0 ? "+" : ""}{changePct.toFixed(2)}%
            </span>
            <span className="rounded-full border border-[#1f2a3d] bg-[#0b1220] px-2 py-1 font-mono text-[11px] text-[#93a4ba]">
              {timeframeLabel(timeframe)} / {rangeLabel(range)}
            </span>
            <span className="rounded-full border border-[#1f2a3d] bg-[#0b1220] px-2 py-1 font-mono text-[11px] text-[#e5eefb]">
              最新 {formatChartNumber(latestPrice)}
            </span>
          </div>
          <div className="mt-2 flex gap-3 overflow-x-auto whitespace-nowrap pb-1">
            <ChartStat label="开" value={focus?.open} />
            <ChartStat label="高" value={focus?.high} tone="good" />
            <ChartStat label="低" value={focus?.low} tone="bad" />
            <ChartStat label="收" value={focus?.close} tone={changePct >= 0 ? "good" : "bad"} />
            <ChartStat label="量" value={focus?.volume} compact />
            <span className="font-mono text-[#93a4ba]">{focus ? formatChartTime(focus.time) : "--"}</span>
          </div>
        </div>

        <div className="min-w-0 space-y-2 sm:flex sm:flex-wrap sm:items-start sm:gap-2 sm:space-y-0">
          {timeframeOptions.length ? (
            <ToolbarGroup label="周期">
              {timeframeOptions.map((item) => (
                <ChartButton key={item} active={timeframe === item} onClick={() => onTimeframeChange?.(item)}>
                  {timeframeLabel(item)}
                </ChartButton>
              ))}
            </ToolbarGroup>
          ) : null}
          <ToolbarGroup label="区间">
            {RANGE_OPTIONS.map((item) => (
              <ChartButton key={item} active={range === item} onClick={() => setRange(item)}>
                {rangeLabel(item)}
              </ChartButton>
            ))}
          </ToolbarGroup>
          <ToolbarGroup label="缩放">
            <ChartButton active={false} onClick={() => zoomChart(chartRef.current, 0.7)}>放大</ChartButton>
            <ChartButton active={false} onClick={() => zoomChart(chartRef.current, 1.45)}>缩小</ChartButton>
            <ChartButton active={false} onClick={resetRange}>重置</ChartButton>
            <ChartButton active={false} onClick={() => chartRef.current?.timeScale().scrollToRealTime()}>最新</ChartButton>
          </ToolbarGroup>
        </div>
      </div>

      <div className="flex flex-wrap gap-1 border-b border-[#101827] bg-[#050b14] px-3 py-2">
        <LayerButton active={layers.kc} onClick={() => toggleLayer("kc")}>KC</LayerButton>
        <LayerButton active={layers.volume} onClick={() => toggleLayer("volume")}>成交量</LayerButton>
        <LayerButton active={layers.orders} onClick={() => toggleLayer("orders")}>订单</LayerButton>
        <LayerButton active={layers.ai} onClick={() => toggleLayer("ai")}>AI 决策</LayerButton>
        <LayerButton active={layers.dense} onClick={() => toggleLayer("dense")}>密集区</LayerButton>
        <LayerButton active={layers.levels} onClick={() => toggleLayer("levels")}>区间高低</LayerButton>
        <LayerButton active={markerDensity === "full"} onClick={() => setMarkerDensity((value) => value === "compact" ? "full" : "compact")}>
          {markerDensity === "compact" ? "精简标记" : "全部标记"}
        </LayerButton>
        <LayerButton active={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? "退出全屏" : "全屏"}</LayerButton>
      </div>

      <div className="relative flex-1 bg-[#050b14]">
        {chartCandles.length ? null : (
          <div className="absolute inset-0 z-10 grid place-items-center bg-[#050b14]/88 text-sm text-[#93a4ba]">等待 K 线数据加载</div>
        )}
        <div ref={rootRef} style={{ height: effectiveHeight }} className="w-full" />
      </div>

      <div className="grid gap-2 border-t border-[#1f2a3d] bg-[#07111f] px-3 py-2 text-[11px] text-[#93a4ba] lg:grid-cols-3">
        <div>可视区间：{visibleCandles.length} 根 K 线，高 {formatChartNumber(visibleStats.high)}，低 {formatChartNumber(visibleStats.low)}。</div>
        <div>默认只显示 K 线、KC、成交量和真实订单；AI、密集区、区间线按需开启。</div>
        <div>标记：订单 {Math.min(allOrderMarkers.length, markerLimit)}/{allOrderMarkers.length}，AI {Math.min(allDecisionMarkers.length, markerLimit)}/{allDecisionMarkers.length}。</div>
      </div>
    </div>
  );
}

function ToolbarGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      <span className="flex h-8 shrink-0 items-center rounded-lg border border-[#1f2a3d] bg-[#0b1220] px-2 text-[10px] text-[#93a4ba]">{label}</span>
      {children}
    </div>
  );
}

function ChartButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`h-8 shrink-0 rounded-lg border px-2.5 text-[11px] transition ${active ? "border-[#60a5fa] bg-[#1d4ed8] text-white shadow-sm" : "border-[#253348] bg-[#0b1220] text-[#cbd5e1] hover:border-[#64748b] hover:text-white"}`}
    >
      {children}
    </button>
  );
}

function LayerButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`h-7 rounded-full border px-3 text-[11px] transition ${active ? "border-[#3b82f6] bg-[#0f2f65] text-white" : "border-[#1f2a3d] bg-[#07111f] text-[#93a4ba] hover:border-[#475569]"}`}
    >
      {children}
    </button>
  );
}

function ChartStat({ label, value, tone = "default", compact = false }: { label: string; value: unknown; tone?: "default" | "good" | "bad"; compact?: boolean }) {
  const color = tone === "good" ? "text-[#22c55e]" : tone === "bad" ? "text-[#f43f5e]" : "text-[#e2e8f0]";
  const formatted = typeof value === "string" ? value : formatChartNumber(value, compact ? 0 : 2);
  return <span className={`${color} font-mono`}><span className="text-[#93a4ba]">{label}</span> {formatted}</span>;
}

function formatChartNumber(value: unknown, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function rangeLabel(value: ChartRange) {
  const labels: Record<ChartRange, string> = {
    "1d": "1D",
    "7d": "7D",
    "30d": "30D",
    "90d": "90D",
    all: "ALL",
  };
  return labels[value];
}

function timeframeLabel(value: string) {
  const labels: Record<string, string> = {
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
    "1w": "1W",
    "1M": "1M",
  };
  return labels[value] || value;
}

type VisibleWindowStats = {
  high: number;
  low: number;
  highTime: string;
  lowTime: string;
};

function visibleWindow(candles: Candle[], range: ChartRange, timeframe: string) {
  if (range === "all") return candles;
  const bars = barsForRange(range, timeframe);
  return candles.slice(-Math.min(candles.length, bars));
}

function barsForRange(range: ChartRange, timeframe: string) {
  const seconds = rangeSeconds(range);
  return Math.max(20, Math.ceil(seconds / timeframeSeconds(timeframe)));
}

function rangeSeconds(range: ChartRange) {
  if (range === "1d") return 86400;
  if (range === "7d") return 7 * 86400;
  if (range === "30d") return 30 * 86400;
  if (range === "90d") return 90 * 86400;
  return Number.POSITIVE_INFINITY;
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
  return `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function formatChartTimeFromSeconds(seconds: number) {
  return formatChartTime(new Date(seconds * 1000).toISOString());
}

function applyVisibleRange(chart: IChartApi | null, candleData: Array<{ time: UTCTimestamp }>, range: ChartRange, timeframe: string) {
  if (!chart || !candleData.length) return;
  if (range === "all") {
    chart.timeScale().fitContent();
    return;
  }
  const bars = barsForRange(range, timeframe);
  const visible = candleData.slice(-Math.min(candleData.length, bars));
  const from = visible[0]?.time;
  const to = visible.at(-1)?.time;
  if (from && to) chart.timeScale().setVisibleRange({ from, to });
}

function zoomChart(chart: IChartApi | null, factor: number) {
  if (!chart) return;
  const logicalRange = chart.timeScale().getVisibleLogicalRange();
  if (!logicalRange) return;
  const width = Math.max(8, logicalRange.to - logicalRange.from);
  const nextWidth = Math.min(Math.max(width * factor, 12), 2500);
  const center = (logicalRange.from + logicalRange.to) / 2;
  chart.timeScale().setVisibleLogicalRange({ from: center - nextWidth / 2, to: center + nextWidth / 2 });
}

function sanitizeCandles(candles: Candle[]): Candle[] {
  const byTime = new Map<number, Candle>();
  for (const item of candles) {
    const time = parseTimestamp(item.time);
    const open = Number(item.open);
    const high = Number(item.high);
    const low = Number(item.low);
    const close = Number(item.close);
    if (time === null || ![open, high, low, close].every((value) => Number.isFinite(value) && value > 0)) {
      continue;
    }
    const normalizedHigh = Math.max(high, open, close);
    const normalizedLow = Math.min(low, open, close);
    byTime.set(Number(time), {
      ...item,
      time: new Date(Number(time) * 1000).toISOString(),
      open,
      high: normalizedHigh,
      low: normalizedLow,
      close,
      volume: Number.isFinite(Number(item.volume)) && Number(item.volume) >= 0 ? Number(item.volume) : 0,
    });
  }
  return Array.from(byTime.entries())
    .sort(([left], [right]) => left - right)
    .map(([, item]) => item);
}

function toCandlePoint(item: Candle) {
  return {
    time: toTimestamp(item.time),
    open: item.open,
    high: item.high,
    low: item.low,
    close: item.close,
  };
}

function toVolumePoint(item: Candle) {
  return {
    time: toTimestamp(item.time),
    value: item.volume,
    color: item.close >= item.open ? "rgba(34, 197, 94, 0.32)" : "rgba(244, 63, 94, 0.32)",
  };
}

function lineData(candles: Candle[], values: number[]) {
  return candles
    .map((item, idx) => ({
      time: toTimestamp(item.time),
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
      const payload = objectPayload(row.payload);
      const side = String(payload.side || payload.action || "").toLowerCase();
      if (!side) return null;
      const isBuy = side === "buy" || side.includes("long");
      const isSell = side === "sell" || side.includes("short") || side.includes("close");
      if (!isBuy && !isSell) return null;
      const timestamp = parseTimestamp(row.created_at || String(payload.created_at || ""));
      if (!timestamp) return null;
      return {
        time: timestamp,
        position: isBuy ? "belowBar" as const : "aboveBar" as const,
        color: isBuy ? "#22c55e" : "#f43f5e",
        shape: isBuy ? "arrowUp" as const : "arrowDown" as const,
        text: isBuy ? "买入/做多" : side.includes("close") ? "平仓" : "卖出/做空",
      };
    })
    .filter((item): item is NonNullable<typeof item> => Boolean(item));
}

function decisionMarkers(decisions: DbRow[]) {
  return decisions
    .map((row) => {
      const payload = objectPayload(row.payload);
      if (payload.review_type || payload.no_order_submitted) return null;
      const risk = objectPayload(payload.risk);
      const ai = objectPayload(payload.ai);
      const confidence = readNumber(payload.confidence ?? ai.confidence ?? risk.confidence);
      const action = String(payload.action_suggestion || payload.veto_action || risk.action || payload.action || "").toLowerCase();
      const tier = String(risk.position_tier || payload.position_tier || "").toLowerCase();
      const color = action === "block" || tier === "block" ? "#f59e0b" : action === "reduce" ? "#f97316" : "#60a5fa";
      const timestamp = parseTimestamp(row.created_at);
      if (!timestamp) return null;
      return {
        time: timestamp,
        position: "inBar" as const,
        color,
        shape: "circle" as const,
        text: Number.isFinite(confidence) ? `AI ${Math.round(confidence * 100)}%` : "AI",
      };
    })
    .filter((item): item is NonNullable<typeof item> => Boolean(item));
}

function denseZoneLines(zone?: DenseZonePayload) {
  if (!zone) return [];
  const lines: Array<{ price: number; color: string; title: string; lineWidth?: 1 | 2 | 3 | 4 }> = [];
  const add = (price: unknown, color: string, title: string, lineWidth: 1 | 2 | 3 | 4 = 1) => {
    const value = Number(price);
    if (Number.isFinite(value) && value > 0) lines.push({ price: value, color, title, lineWidth });
  };
  add(zone.zone_high ?? zone.vah, "#f59e0b", "密集区上沿", 2);
  add(zone.zone_mid ?? zone.poc, "#3b82f6", "POC / 中位", 1);
  add(zone.zone_low ?? zone.val, "#22c55e", "密集区下沿", 2);
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

function toTimestamp(value: unknown) {
  return (parseTimestamp(value) || Math.floor(Date.now() / 1000)) as UTCTimestamp;
}

function parseTimestamp(value: unknown): UTCTimestamp | null {
  if (typeof value !== "string" || !value) return null;
  const ms = new Date(value).getTime();
  if (!Number.isFinite(ms)) return null;
  return Math.floor(ms / 1000) as UTCTimestamp;
}

function timeframeSeconds(timeframe: string) {
  const raw = String(timeframe || "1h");
  const value = Number.parseInt(raw.slice(0, -1), 10);
  const amount = Number.isFinite(value) && value > 0 ? value : 1;
  const unit = raw.slice(-1);
  if (unit === "m") return amount * 60;
  if (unit === "h") return amount * 3600;
  if (unit === "d") return amount * 86400;
  if (unit === "w") return amount * 7 * 86400;
  if (unit === "M") return amount * 30 * 86400;
  return 3600;
}

function readNumericParam(params: Record<string, unknown>, keys: string[], fallback: number) {
  for (const key of keys) {
    const value = readNumber(params[key]);
    if (Number.isFinite(value) && value > 0) return value;
  }
  return fallback;
}

function readNumber(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) ? number : Number.NaN;
}

function objectPayload(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
