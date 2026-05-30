import type { ReactNode } from "react";

export const mono = "font-mono tabular-nums";
export const input =
  "h-9 rounded-lg border border-[#263246] bg-[#0b1220] px-2 text-xs text-[#e5eefb] outline-none placeholder:text-[#64748b] focus:border-[#3b82f6] focus:ring-2 focus:ring-[#1d4ed8]/30";
export const button =
  "inline-flex h-9 items-center justify-center gap-1 rounded-lg border border-[#263246] bg-[#111827] px-3 text-xs font-medium text-[#dbeafe] hover:border-[#3b82f6] hover:text-white disabled:opacity-50";
export const danger =
  "inline-flex h-9 items-center justify-center gap-1 rounded-lg border border-[#7f1d1d] bg-[#2a0f14] px-3 text-xs font-medium text-[#fb7185] hover:border-[#fb7185] hover:text-[#fecdd3] disabled:opacity-50";

export const num = (value: unknown, digits = 2): string => {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "--";
};

export const pct = (value: unknown): string => `${num(value, 2)}%`;
export const shortSymbol = (symbol: string): string => symbol.split("/")[0] || symbol;
export const errText = (error: unknown): string => (error instanceof Error ? error.message : String(error || "unknown error"));

export function Surface({ title, action, children }: { title: ReactNode; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="min-w-0 rounded-xl border border-[#263246] bg-[#0b1220] shadow-[0_18px_44px_rgba(0,0,0,0.28)]">
      <div className="flex h-11 items-center justify-between border-b border-[#1f2a3d] px-4 text-[12px] font-semibold text-[#cbd5e1]">
        <div className="flex items-center gap-2">{title}</div>
        {action}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Metric({ label, value, tone = "default" }: { label: string; value: string; tone?: "default" | "good" | "bad" | "warn" }) {
  const toneClass =
    tone === "good" ? "text-[#22c55e]" : tone === "bad" ? "text-[#fb7185]" : tone === "warn" ? "text-[#facc15]" : "text-[#e5eefb]";
  return (
    <div className="rounded-lg border border-[#263246] bg-[#101a2d] p-3">
      <div className="text-[11px] text-[#94a3b8]">{label}</div>
      <div className={`${mono} mt-1 truncate text-sm font-semibold ${toneClass}`}>{value}</div>
    </div>
  );
}

export function JsonBlock({ data, maxHeight = "max-h-80" }: { data: unknown; maxHeight?: string }) {
  return (
    <pre className={`${maxHeight} overflow-auto whitespace-pre-wrap rounded-xl border border-[#263246] bg-[#07111f] p-3 text-[11px] text-[#cbd5e1]`}>
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}
