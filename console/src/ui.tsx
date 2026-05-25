import type { ReactNode } from "react";

export const mono = "font-mono tabular-nums";
export const input =
  "h-9 rounded-lg border border-[#d8e1ee] bg-white px-2 text-xs text-[#172033] outline-none focus:border-[#2454ff] focus:ring-2 focus:ring-[#dce6ff]";
export const button =
  "inline-flex h-9 items-center justify-center gap-1 rounded-lg border border-[#d8e1ee] bg-white px-3 text-xs font-medium text-[#2f3b52] hover:border-[#2454ff] hover:text-[#2454ff] disabled:opacity-50";
export const danger =
  "inline-flex h-9 items-center justify-center gap-1 rounded-lg border border-[#ffd1d6] bg-[#fff1f2] px-3 text-xs font-medium text-[#e11d48] hover:border-[#fb7185] disabled:opacity-50";

export const num = (value: unknown, digits = 2): string => {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "--";
};

export const pct = (value: unknown): string => `${num(value, 2)}%`;
export const shortSymbol = (symbol: string): string => symbol.split("/")[0] || symbol;
export const errText = (error: unknown): string => (error instanceof Error ? error.message : String(error || "unknown error"));

export function Surface({ title, action, children }: { title: ReactNode; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="min-w-0 rounded-2xl border border-[#d9e2ef] bg-white shadow-[0_12px_35px_rgba(26,42,68,0.06)]">
      <div className="flex h-12 items-center justify-between border-b border-[#e6edf5] px-4 text-[12px] font-semibold uppercase tracking-wide text-[#53627a]">
        <div className="flex items-center gap-2">{title}</div>
        {action}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Metric({ label, value, tone = "default" }: { label: string; value: string; tone?: "default" | "good" | "bad" | "warn" }) {
  const toneClass =
    tone === "good" ? "text-[#0a9f5a]" : tone === "bad" ? "text-[#e11d48]" : tone === "warn" ? "text-[#b7791f]" : "text-[#172033]";
  return (
    <div className="rounded-xl border border-[#dfe7f1] bg-[#f8fbff] p-3">
      <div className="text-[10px] uppercase text-[#7b8798]">{label}</div>
      <div className={`${mono} mt-1 truncate text-sm font-semibold ${toneClass}`}>{value}</div>
    </div>
  );
}

export function JsonBlock({ data, maxHeight = "max-h-80" }: { data: unknown; maxHeight?: string }) {
  return (
    <pre className={`${maxHeight} overflow-auto whitespace-pre-wrap rounded-xl border border-[#dfe7f1] bg-[#f8fbff] p-3 text-[11px] text-[#53627a]`}>
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}
