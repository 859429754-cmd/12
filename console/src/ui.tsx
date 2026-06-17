import type { ReactNode } from "react";

export const mono = "font-mono tabular-nums";
export const input =
  "h-9 rounded-lg border border-[#263246] bg-[#0b1220] px-2 text-xs text-[#e5eefb] outline-none placeholder:text-[#64748b] focus:border-[#3b82f6] focus:ring-2 focus:ring-[#1d4ed8]/30";
export const button =
  "inline-flex h-9 items-center justify-center gap-1 rounded-lg border border-[#263246] bg-[#111827] px-3 text-xs font-medium text-[#dbeafe] hover:border-[#3b82f6] hover:text-white disabled:opacity-50";
export const danger =
  "inline-flex h-9 items-center justify-center gap-1 rounded-lg border border-[#7f1d1d] bg-[#2a0f14] px-3 text-xs font-medium text-[#fb7185] hover:border-[#fb7185] hover:text-[#fecdd3] disabled:opacity-50";

export const num = (value: unknown, digits = 2): string => {
  if (value === null || value === undefined) return "--";
  if (typeof value === "string" && value.trim() === "") return "--";
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "--";
};

export const pct = (value: unknown): string => `${num(value, 2)}%`;
export const shortSymbol = (symbol: string): string => symbol.split("/")[0] || symbol;
export const errText = (error: unknown): string => {
  const fallback = error instanceof Error ? error.message : String(error || "unknown error");
  const maybe = error as { status?: number; body?: unknown } | null;
  const body = maybe && typeof maybe === "object" ? maybe.body : undefined;
  if (body && typeof body === "object") {
    const item = body as Record<string, unknown>;
    const detail = item.detail;
    if (detail === "auth_required") return "请先登录 AI 量化控制台账号。";
    if (detail === "permission_denied") return "当前账号没有执行该操作的权限。";
    if (typeof detail === "string" && detail.trim()) return detail;
    if (typeof item.message === "string" && item.message.trim()) return item.message;
    if (Array.isArray(detail)) {
      const first = detail[0] as Record<string, unknown> | undefined;
      if (first?.msg) return `请求参数错误：${String(first.msg)}`;
    }
  }
  if (fallback === "auth_required") return "请先登录 AI 量化控制台账号。";
  if (fallback === "permission_denied") return "当前账号没有执行该操作的权限。";
  return fallback;
};

export function Surface({ title, action, children }: { title: ReactNode; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="min-w-0 rounded-xl border border-[#263246] bg-[#0b1220] shadow-[0_18px_44px_rgba(0,0,0,0.28)]">
      <div className="flex min-h-11 flex-wrap items-center justify-between gap-2 border-b border-[#1f2a3d] px-3 py-2 text-[12px] font-semibold text-[#cbd5e1] sm:px-4">
        <div className="flex min-w-0 items-center gap-2">{title}</div>
        {action ? <div className="min-w-0">{action}</div> : null}
      </div>
      <div className="p-3 sm:p-4">{children}</div>
    </section>
  );
}

export function Metric({ label, value, tone = "default" }: { label: string; value: string; tone?: "default" | "good" | "bad" | "warn" }) {
  const toneClass =
    tone === "good" ? "text-[#22c55e]" : tone === "bad" ? "text-[#fb7185]" : tone === "warn" ? "text-[#facc15]" : "text-[#e5eefb]";
  return (
    <div className="rounded-lg border border-[#263246] bg-[#101a2d] p-3">
      <div className="text-[11px] text-[#94a3b8]">{label}</div>
      <div className={`${mono} mt-1 min-w-0 break-words text-sm font-semibold ${toneClass}`}>{value}</div>
    </div>
  );
}

export function JsonBlock({ data, maxHeight = "max-h-80" }: { data: unknown; maxHeight?: string }) {
  return (
    <pre className={`${maxHeight} max-w-full overflow-auto whitespace-pre-wrap break-words rounded-xl border border-[#263246] bg-[#07111f] p-3 text-[11px] text-[#cbd5e1]`}>
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}
