import { useEffect, useRef } from "react";
import { createChart, ColorType, type IChartApi, type IPriceLine, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
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

  useEffect(() => {
    if (!rootRef.current) return;
    const chart = createChart(rootRef.current, {
      height,
      layout: { background: { type: ColorType.Solid, color: "#ffffff" }, textColor: "#53627a" },
      grid: { vertLines: { color: "#eef2f7" }, horzLines: { color: "#eef2f7" } },
      rightPriceScale: { borderColor: "#d9e2ef", scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: { borderColor: "#d9e2ef", timeVisible: true, secondsVisible: false },
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
    const emaSeries = chart.addLineSeries({ color: "#1f2937", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    const kcMidSeries = chart.addLineSeries({ color: "#64748b", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const kcUpperSeries = chart.addLineSeries({ color: "#2454ff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const kcLowerSeries = chart.addLineSeries({ color: "#2454ff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });

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
    };
  }, [height]);

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
    const volumeData = candles.map((item) => ({
      time: Math.floor(new Date(item.time).getTime() / 1000) as UTCTimestamp,
      value: item.volume,
      color: item.close >= item.open ? "rgba(10, 159, 90, 0.35)" : "rgba(225, 29, 72, 0.35)",
    }));
    candleSeries.setData(candleData);
    volumeSeries.setData(volumeData);
    const params = profile?.params || {};
    const emaLen = Number(params.ema_length || 89);
    const kcLen = Number(params.kc_length || 20);
    const atrLen = Number(params.atr_length || 14);
    const kcMult = Number(params.kc_scalar || 2.8);
    const closes = candles.map((item) => item.close);
    const emaValues = ema(closes, emaLen);
    const kcMid = ema(closes, kcLen);
    const atrValues = atr(candles, atrLen);
    emaRef.current?.setData(lineData(candles, emaValues));
    kcMidRef.current?.setData(lineData(candles, kcMid));
    kcUpperRef.current?.setData(lineData(candles, kcMid.map((value, idx) => value + atrValues[idx] * kcMult)));
    kcLowerRef.current?.setData(lineData(candles, kcMid.map((value, idx) => value - atrValues[idx] * kcMult)));
    candleSeries.setMarkers([
      ...orderMarkers(orders),
      ...decisionMarkers(decisions),
    ].sort((a, b) => Number(a.time) - Number(b.time)).slice(-120));
    for (const line of denseLinesRef.current) {
      candleSeries.removePriceLine(line);
    }
    denseLinesRef.current = denseZoneLines(denseZone).map((item) => candleSeries.createPriceLine(item));
    chartRef.current?.timeScale().fitContent();
  }, [candles, decisions, denseZone, orders, profile]);

  return <div ref={rootRef} style={{ height }} className="w-full" />;
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
