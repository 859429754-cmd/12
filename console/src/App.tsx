import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  Bot,
  BrainCircuit,
  Database,
  FlaskConical,
  Gauge,
  KeyRound,
  LineChart,
  Menu,
  Newspaper,
  Power,
  RefreshCcw,
  ServerCog,
  ShieldCheck,
  Wallet,
} from "lucide-react";
import type { ReactNode } from "react";
import { api } from "./lib/api";
import { MarketChart } from "./MarketChart";
import type {
  ApiList,
  BacktestJob,
  BacktestResult,
  BacktestTrade,
  Candle,
  CandleResponse,
  ConsoleSession,
  DenseZonePayload,
  DbRow,
  ExecutionAccountSlot,
  MarketSymbolsResponse,
  MarketTickerResponse,
  NewsResponse,
  OptimizationResult,
  PlatformOverview,
  SystemReadiness,
  StatusResponse,
  StrategyProfile,
  WorkerHeartbeatDetail,
  WorkspaceId,
} from "./types";
import { JsonBlock, Metric, Surface, button, danger, errText, input, mono, num, pct, shortSymbol } from "./ui";

const DEFAULT_SYMBOL = "ETH/USDT:USDT";
const WORKSPACE_IDS: WorkspaceId[] = ["dashboard", "market", "strategy", "ai", "agent", "execution", "data"];
const CHART_TIMEFRAMES = ["15m", "1h", "4h", "1d", "1w", "1M"];

function chartCandleLimits(timeframe: string) {
  const map: Record<string, { fast: number; full: number }> = {
    "15m": { fast: 1200, full: 5000 },
    "1h": { fast: 1200, full: 5000 },
    "4h": { fast: 900, full: 3000 },
    "1d": { fast: 700, full: 1500 },
    "1w": { fast: 260, full: 520 },
    "1M": { fast: 120, full: 240 },
  };
  return map[timeframe] || map["1h"];
}

export function App() {
  const [workspace, setWorkspace] = useState<WorkspaceId>(() => readWorkspaceHash());
  const [session, setSession] = useState<ConsoleSession | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [platform, setPlatform] = useState<PlatformOverview | null>(null);
  const [balance, setBalance] = useState<Record<string, unknown> | null>(null);
  const [followerBalance, setFollowerBalance] = useState<Record<string, unknown> | null>(null);
  const [markets, setMarkets] = useState<MarketSymbolsResponse>({ items: [] });
  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL);
  const [timeframe, setTimeframe] = useState("1h");
  const [source, setSource] = useState("binance");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [ticker, setTicker] = useState<MarketTickerResponse | null>(null);
  const [orders, setOrders] = useState<Array<DbRow>>([]);
  const [positions, setPositions] = useState<Array<DbRow>>([]);
  const [decisions, setDecisions] = useState<Array<DbRow>>([]);
  const [riskSummary, setRiskSummary] = useState<Record<string, unknown> | null>(null);
  const [readiness, setReadiness] = useState<SystemReadiness | null>(null);
  const [accountSlots, setAccountSlots] = useState<ExecutionAccountSlot[]>([]);
  const [denseZone, setDenseZone] = useState<DbRow<DenseZonePayload> | null>(null);
  const [news, setNews] = useState<NewsResponse>({ items: [] });
  const [warning, setWarning] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const capabilities = session?.user?.capabilities;
  const visibleSlots = session?.user?.visible_account_slots || ["trend", "follower", "range"];
  const isAdmin = Boolean(capabilities?.manage_strategy_parameters);

  const symbols = useMemo(() => {
    const fromMarkets = markets.items.map((item) => item.symbol);
    const fromStatus = status?.symbols?.map((item) => item.symbol) || [];
    const fromProfiles = platform?.strategy_profiles.map((item) => item.symbol) || [];
    return Array.from(new Set([...fromMarkets, ...fromStatus, ...fromProfiles, DEFAULT_SYMBOL]));
  }, [markets, platform, status]);

  const selectedProfile = platform?.strategy_profiles.find((item) => item.symbol === symbol);
  const displayCandles = useMemo(() => overlayRealtimePriceOnCandles(candles, ticker, timeframe), [candles, ticker, timeframe]);

  const refreshTicker = useCallback(async () => {
    try {
      const nextTicker = await api<MarketTickerResponse>(
        `/api/market/ticker?symbol=${encodeURIComponent(symbol)}&source=${source === "cryptocompare" ? "auto" : source}`,
        { retries: 0, timeoutMs: 5000 },
      );
      setTicker(nextTicker);
    } catch {
      setTicker({ symbol, source: "unavailable", last: null, warning: "实时价格接口暂时未返回。" });
    }
  }, [source, symbol]);

  const load = useCallback(async () => {
    if (session?.auth_required && !session.authenticated) return;
    setWarning("");
    try {
      const safe = async <T,>(promise: Promise<T>, fallback: T): Promise<T> => {
        try {
          return await promise;
        } catch {
          return fallback;
        }
      };
      void safe(api<NewsResponse>("/api/news/latest?limit=1&compact=true", { retries: 0, timeoutMs: 8000 }), {
        items: [],
        timeline: [],
        warnings: ["新闻接口暂时未返回，已保留上一轮界面状态。"],
      }).then(setNews);
      void refreshTicker();
      const candleBase = `/api/market/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&source=${source}`;
      const candleLimits = chartCandleLimits(timeframe);
      void safe(api<CandleResponse>(`${candleBase}&limit=${candleLimits.fast}&closed_only=false`, { retries: 0, timeoutMs: 9000 }), {
        items: [],
        warning: "K线接口暂时未返回，已保留上一轮界面状态。",
      }).then((nextCandles) => {
        if (nextCandles.items?.length) {
          setCandles((current) => fresherCandles(current, nextCandles.items || []));
          setWarning(nextCandles.warning || "");
        } else {
          setWarning(nextCandles.warning || "");
        }
      });
      void safe(api<CandleResponse>(`${candleBase}&limit=${candleLimits.full}&closed_only=false`, { retries: 0, timeoutMs: 25000 }), {
        items: [],
        warning: "",
      }).then((nextCandles) => {
        if (nextCandles.items?.length) {
          setCandles((current) => fresherCandles(current, nextCandles.items || []));
          setWarning(nextCandles.warning || "");
        }
      });
      const [
        nextStatus,
        nextPlatform,
        nextReadiness,
        nextMarkets,
        nextBalance,
        nextFollowerBalance,
        nextPositions,
        nextRiskSummary,
        nextAccountSlots,
        nextOrders,
        nextDecisions,
        nextDenseZone,
      ] =
        await Promise.all([
          api<StatusResponse>("/api/status", { retries: 1 }),
          api<PlatformOverview>("/api/platform/overview", { retries: 1 }),
          api<SystemReadiness>("/api/system/readiness", { retries: 1 }),
          api<MarketSymbolsResponse>("/api/markets/symbols", { retries: 1 }),
          api<Record<string, unknown>>("/api/account/balance", { retries: 1 }),
          safe(api<Record<string, unknown>>("/api/account/balance?account_slot=follower", { retries: 1 }), {
            ok: false,
            account_slot: "follower",
            message: "账号2未配置或暂时无法读取。",
          }),
          api<ApiList>("/api/positions?limit=50", { retries: 1 }),
          api<Record<string, unknown>>("/api/risk/summary", { retries: 1 }),
          safe(api<{ items: ExecutionAccountSlot[] }>("/api/execution/accounts", { retries: 1 }), { items: [] }),
          api<ApiList>(`/api/orders?limit=80&symbol=${encodeURIComponent(symbol)}`, { retries: 1 }),
          api<ApiList>(`/api/decisions?limit=80&symbol=${encodeURIComponent(symbol)}`, { retries: 1 }),
          safe(api<{ item: DbRow<DenseZonePayload> | null }>(`/api/dense-zones/latest?symbol=${encodeURIComponent(symbol)}`, { retries: 1 }), { item: null }),
        ]);
      setStatus(nextStatus);
      setPlatform(nextPlatform);
      setReadiness(nextReadiness);
      setMarkets(nextMarkets);
      setBalance(nextBalance);
      setFollowerBalance(nextFollowerBalance);
      setPositions(nextPositions.items || []);
      setRiskSummary(nextRiskSummary);
      setAccountSlots(nextAccountSlots.items || []);
      setOrders(nextOrders.items || []);
      setDecisions(nextDecisions.items || []);
      setDenseZone(nextDenseZone.item || null);
    } catch (error) {
      setWarning(errText(error));
    }
  }, [refreshTicker, session?.auth_required, session?.authenticated, source, symbol, timeframe]);

  useEffect(() => {
    void api<ConsoleSession>("/api/auth/session", { retries: 0, timeoutMs: 5000 })
      .then(setSession)
      .catch(() => setSession({ ok: false, auth_required: true, authenticated: false, user: null }));
  }, []);

  useEffect(() => {
    if (!session) return;
    void load();
    const id = window.setInterval(() => void load(), 60_000);
    return () => window.clearInterval(id);
  }, [load, session]);

  useEffect(() => {
    if (!session) return;
    if (session?.auth_required && !session.authenticated) return;
    void refreshTicker();
    const id = window.setInterval(() => void refreshTicker(), 10_000);
    return () => window.clearInterval(id);
  }, [refreshTicker, session?.auth_required, session?.authenticated]);

  useEffect(() => {
    if (window.location.hash.replace("#", "") !== workspace) {
      window.history.replaceState(null, "", `#${workspace}`);
    }
  }, [workspace]);

  useEffect(() => {
    const syncWorkspaceFromHash = () => {
      setWorkspace(readWorkspaceHash());
    };
    window.addEventListener("hashchange", syncWorkspaceFromHash);
    window.addEventListener("popstate", syncWorkspaceFromHash);
    return () => {
      window.removeEventListener("hashchange", syncWorkspaceFromHash);
      window.removeEventListener("popstate", syncWorkspaceFromHash);
    };
  }, []);

  const postAction = async (path: string, body: Record<string, unknown>) => {
    setBusy(true);
    setMessage("");
    try {
      const result = await api<Record<string, unknown>>(path, { method: "POST", body: JSON.stringify(body), timeoutMs: 15000 });
      setMessage(String(result.message || "ok"));
      await load();
    } catch (error) {
      setMessage(errText(error));
    } finally {
      setBusy(false);
    }
  };

  const showRightRail = workspace !== "dashboard";

  const login = async (username: string, password: string) => {
    setBusy(true);
    setMessage("");
    try {
      const nextSession = await api<ConsoleSession>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
        retries: 0,
        timeoutMs: 8000,
      });
      setSession(nextSession);
      setMessage("登录成功。");
      await load();
    } catch (error) {
      setMessage(errText(error));
    } finally {
      setBusy(false);
    }
  };

  const logout = async () => {
    await api<Record<string, unknown>>("/api/auth/logout", { method: "POST", body: JSON.stringify({}), retries: 0 });
    setSession({ ok: true, auth_required: true, authenticated: false, user: null });
  };

  if (!session) {
    return <div className="min-h-[100dvh] bg-[#07111f] text-[#e5eefb]" />;
  }

  if (session.auth_required && !session.authenticated) {
    return <LoginScreen busy={busy} message={message} onLogin={login} />;
  }

  return (
    <main className="flex min-h-[100dvh] flex-col overflow-hidden bg-[#07111f] text-[#e5eefb] md:grid md:h-screen md:grid-cols-[68px_minmax(0,1fr)] xl:grid-cols-[252px_minmax(0,1fr)]">
      <ShellNav
        platform={platform}
        workspace={workspace}
        setWorkspace={setWorkspace}
        symbol={symbol}
        setSymbol={setSymbol}
        symbols={symbols}
        profile={selectedProfile}
        status={status}
        balance={balance}
        busy={busy}
        message={message}
        session={session}
        logout={logout}
        postAction={postAction}
      />
      <section className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      <TopBar
        platform={platform}
        status={status}
        workspace={workspace}
        setWorkspace={setWorkspace}
        refresh={load}
        warning={warning}
        session={session}
        logout={logout}
      />
      <div className={`grid min-h-0 flex-1 gap-3 overflow-auto px-2 py-3 pb-24 sm:gap-4 sm:p-4 ${showRightRail ? "grid-cols-[minmax(0,1fr)] xl:grid-cols-[minmax(0,1fr)_320px]" : "grid-cols-[minmax(0,1fr)]"}`}>
        <WorkspaceBody
          workspace={workspace}
          symbol={symbol}
          setSymbol={setSymbol}
          symbols={symbols}
          timeframe={timeframe}
          setTimeframe={setTimeframe}
          source={source}
          setSource={setSource}
        candles={displayCandles}
        ticker={ticker}
        warning={warning}
        runtimeStatus={status}
        balance={balance}
        followerBalance={followerBalance}
        markets={markets}
        news={news}
        positions={positions}
        orders={orders}
        accountSlots={accountSlots}
        denseZone={denseZone}
        riskSummary={riskSummary}
        readiness={readiness}
        visibleSlots={visibleSlots}
        isAdmin={isAdmin}
        busy={busy}
        postAction={postAction}
          decisions={decisions}
          platform={platform}
      />
        {showRightRail ? (
          <div className="hidden min-h-0 xl:block">
            <RightRail
              symbol={symbol}
              status={status}
              decisions={decisions}
              positions={positions}
              orders={orders}
              news={news}
              denseZone={denseZone}
              busy={busy}
              postAction={postAction}
            />
          </div>
        ) : null}
      </div>
      </section>
      <MobileBottomNav platform={platform} workspace={workspace} setWorkspace={setWorkspace} />
    </main>
  );
}

function fresherCandles(current: Candle[], candidate: Candle[]) {
  if (!candidate.length) return current;
  if (!current.length) return candidate;
  const currentLast = candleTimeMs(current.at(-1));
  const candidateLast = candleTimeMs(candidate.at(-1));
  if (!Number.isFinite(candidateLast)) return current;
  if (!Number.isFinite(currentLast)) return candidate;
  return candidateLast >= currentLast ? candidate : current;
}

function candleTimeMs(candle?: Candle) {
  if (!candle?.time) return Number.NaN;
  const value = new Date(candle.time).getTime();
  return Number.isFinite(value) ? value : Number.NaN;
}

function readWorkspaceHash(): WorkspaceId {
  const value = window.location.hash.replace("#", "");
  return WORKSPACE_IDS.includes(value as WorkspaceId) ? (value as WorkspaceId) : "dashboard";
}

function workspaceIcon(id: WorkspaceId) {
  const className = "h-4 w-4";
  if (id === "dashboard") return <Gauge className={className} />;
  if (id === "market") return <BarChart3 className={className} />;
  if (id === "strategy") return <FlaskConical className={className} />;
  if (id === "ai") return <BrainCircuit className={className} />;
  if (id === "agent") return <Bot className={className} />;
  if (id === "execution") return <Power className={className} />;
  return <Database className={className} />;
}

function workspaceLabel(id: WorkspaceId, platform: PlatformOverview | null) {
  const zh: Record<WorkspaceId, string> = {
    dashboard: "总览",
    market: "行情图表",
    strategy: "策略与回测",
    ai: "AI 大脑",
    agent: "智能体网关",
    execution: "交易执行",
    data: "快讯与数据",
  };
  return zh[id] || platform?.workspaces.find((item) => item.id === id)?.label || id;
}

function platformShellLabel(value?: string) {
  if (value === "quantdinger_style") return "工作台外壳";
  return value || "--";
}

function platformCoreLabel(value?: string) {
  if (value === "local_ai_quant_trader") return "本地内核";
  return value || "--";
}

function executionModeLabel(value?: string) {
  if (value === "mock") return "模拟";
  if (value === "live") return "实盘";
  return value || "--";
}

function tradeModeLabel(value?: string) {
  if (value === "strategy_confirmed") return "策略确认";
  if (value === "ai_candidate_approval") return "AI候选审批";
  if (value === "pure_ai_paper") return "纯AI纸面";
  return value || "--";
}

function overlayRealtimePriceOnCandles(candles: Candle[], ticker: MarketTickerResponse | null, timeframe: string): Candle[] {
  const price = firstFiniteMarketPrice(ticker?.last, ticker?.mark, ticker?.bid && ticker?.ask ? (Number(ticker.bid) + Number(ticker.ask)) / 2 : null);
  if (!Number.isFinite(price) || !candles.length) return candles;

  const tickerTime = parseTickerTime(ticker?.timestamp);
  const bucketTime = timeframeBucketIso(tickerTime, timeframe);
  const latest = candles.at(-1);
  if (!latest) return candles;

  if (latest.time === bucketTime) {
    const nextLatest: Candle = {
      ...latest,
      high: Math.max(latest.high, price),
      low: Math.min(latest.low, price),
      close: price,
    };
    return [...candles.slice(0, -1), nextLatest];
  }

  const nextCandle: Candle = {
    time: bucketTime,
    open: latest.close,
    high: Math.max(latest.close, price),
    low: Math.min(latest.close, price),
    close: price,
    volume: 0,
  };
  return [...candles, nextCandle];
}

function firstFiniteMarketPrice(...values: unknown[]): number {
  for (const value of values) {
    const number = Number(value);
    if (Number.isFinite(number) && number > 0) return number;
  }
  return Number.NaN;
}

function parseTickerTime(value: unknown): Date {
  const timestamp = typeof value === "string" ? Date.parse(value) : Number.NaN;
  return Number.isFinite(timestamp) ? new Date(timestamp) : new Date();
}

function timeframeBucketIso(date: Date, timeframe: string): string {
  const year = date.getUTCFullYear();
  const month = date.getUTCMonth();
  const day = date.getUTCDate();
  const hour = date.getUTCHours();
  const minute = date.getUTCMinutes();

  if (timeframe === "15m") {
    return new Date(Date.UTC(year, month, day, hour, Math.floor(minute / 15) * 15)).toISOString();
  }
  if (timeframe === "4h") {
    return new Date(Date.UTC(year, month, day, Math.floor(hour / 4) * 4)).toISOString();
  }
  if (timeframe === "1d") {
    return new Date(Date.UTC(year, month, day)).toISOString();
  }
  if (timeframe === "1w") {
    const start = new Date(Date.UTC(year, month, day));
    const dayOfWeek = start.getUTCDay() || 7;
    start.setUTCDate(start.getUTCDate() - dayOfWeek + 1);
    return start.toISOString();
  }
  if (timeframe === "1M") {
    return new Date(Date.UTC(year, month, 1)).toISOString();
  }
  return new Date(Date.UTC(year, month, day, hour)).toISOString();
}

function LoginScreen({
  busy,
  message,
  onLogin,
}: {
  busy: boolean;
  message: string;
  onLogin: (username: string, password: string) => Promise<void>;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  return (
    <main className="grid min-h-[100dvh] place-items-center bg-[#07111f] px-4 text-[#e5eefb]">
      <div className="w-full max-w-md rounded-3xl border border-[#263246] bg-[#0b1220] p-6 shadow-2xl shadow-black/40">
        <div className="flex items-center gap-3">
          <div className="grid h-11 w-11 place-items-center rounded-2xl bg-[#2454ff] text-lg font-black text-white">Q</div>
          <div>
            <div className="text-xl font-bold">AI 量化控制台登录</div>
            <div className="mt-1 text-xs text-[#94a3b8]">账号权限决定可见账户和可执行操作。</div>
          </div>
        </div>
        <div className="mt-6 grid gap-3">
          <input className={`${input} h-12 w-full`} value={username} placeholder="用户名" autoComplete="username" onChange={(event) => setUsername(event.target.value)} />
          <input className={`${input} h-12 w-full`} type="password" value={password} placeholder="密码" autoComplete="current-password" onChange={(event) => setPassword(event.target.value)} />
          <button className={`${button} h-12 justify-center border-[#3b82f6] bg-[#1d4ed8] text-white`} disabled={busy || !username.trim() || !password} onClick={() => onLogin(username, password)}>
            登录
          </button>
        </div>
        {message ? <div className="mt-4 rounded-xl border border-[#263246] bg-[#101a2d] px-3 py-2 text-xs text-[#cbd5e1]">{message}</div> : null}
      </div>
    </main>
  );
}

function ShellNav({
  platform,
  workspace,
  setWorkspace,
  symbol,
  setSymbol,
  symbols,
  profile,
  status,
  balance,
  busy,
  message,
  session,
  logout,
  postAction,
}: {
  platform: PlatformOverview | null;
  workspace: WorkspaceId;
  setWorkspace: (value: WorkspaceId) => void;
  symbol: string;
  setSymbol: (value: string) => void;
  symbols: string[];
  profile: StrategyProfile | undefined;
  status: StatusResponse | null;
  balance: Record<string, unknown> | null;
  busy: boolean;
  message: string;
  session: ConsoleSession;
  logout: () => Promise<void>;
  postAction: (path: string, body: Record<string, unknown>) => Promise<void>;
}) {
  const workspaces = platform?.workspaces?.length ? platform.workspaces : [
    { id: "dashboard" as WorkspaceId, label: "总览" },
    { id: "market" as WorkspaceId, label: "行情图表" },
    { id: "strategy" as WorkspaceId, label: "策略与回测" },
    { id: "ai" as WorkspaceId, label: "AI 大脑" },
    { id: "agent" as WorkspaceId, label: "智能体网关" },
    { id: "execution" as WorkspaceId, label: "交易执行" },
    { id: "data" as WorkspaceId, label: "快讯与数据" },
  ];
  return (
    <aside className="hidden min-h-0 flex-col border-r border-[#1f2a3d] bg-[#08111f] shadow-[10px_0_30px_rgba(0,0,0,0.35)] md:flex">
      <div className="flex h-16 shrink-0 items-center justify-center gap-2 border-b border-[#1f2a3d] px-2 xl:justify-start xl:px-4">
        <div className="grid h-9 w-9 place-items-center rounded-xl bg-[#2454ff] text-base font-black text-white shadow-lg shadow-blue-950/40">
          Q
        </div>
        <div className="hidden min-w-0 xl:block">
          <div className="truncate text-base font-bold tracking-tight text-[#dbeafe]">量化 AI 工作台</div>
          <div className="truncate text-[10px] text-[#94a3b8]">策略内核 / AI 风控</div>
        </div>
      </div>
      <nav className="shrink-0 px-2 py-3">
        {workspaces.map((item) => {
          const active = workspace === item.id;
          return (
            <button
              key={item.id}
              className={`mb-1 flex h-10 w-full items-center justify-center gap-2 rounded-xl px-0 text-left text-sm transition xl:justify-start xl:px-3 ${
                active ? "bg-[#1d4ed8] text-white shadow-inner shadow-blue-950/30" : "text-[#94a3b8] hover:bg-[#111827] hover:text-[#e5eefb]"
              }`}
              onClick={() => setWorkspace(item.id)}
            >
              {workspaceIcon(item.id)}
              <span className="hidden font-medium xl:inline">{workspaceLabel(item.id, platform)}</span>
            </button>
          );
        })}
      </nav>
      <div className="hidden min-h-0 flex-1 overflow-auto border-t border-[#1f2a3d] bg-[#07111f] p-2 xl:block">
        <div className="mb-3 rounded-2xl border border-[#263246] bg-[#101a2d] p-3">
          <div className="text-[11px] text-[#94a3b8]">当前登录</div>
          <div className="mt-1 truncate text-sm font-semibold text-[#e5eefb]">{session.user?.label || session.user?.username || "未登录"}</div>
          <div className="mt-1 text-[11px] text-[#64748b]">{session.user?.role || "unknown"}</div>
          <button className={`${button} mt-3 h-9 w-full justify-center`} onClick={logout}>退出登录</button>
        </div>
        <LeftRail
          workspace={workspace}
          symbol={symbol}
          setSymbol={setSymbol}
          symbols={symbols}
          profile={profile}
          status={status}
          balance={balance}
          busy={busy}
          message={message}
          canControl={Boolean(session.user?.capabilities.manage_runtime)}
          postAction={postAction}
        />
      </div>
    </aside>
  );
}

function TopBar({
  platform,
  status,
  workspace,
  setWorkspace,
  refresh,
  warning,
  session,
  logout,
}: {
  platform: PlatformOverview | null;
  status: StatusResponse | null;
  workspace: WorkspaceId;
  setWorkspace: (value: WorkspaceId) => void;
  refresh: () => void;
  warning: string;
  session: ConsoleSession;
  logout: () => Promise<void>;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const switchWorkspace = (id: WorkspaceId) => {
    setWorkspace(id);
    setMenuOpen(false);
  };
  return (
    <header className="relative flex min-h-14 shrink-0 items-center gap-2 border-b border-[#1f2a3d] bg-[#0b1220] px-2 shadow-sm shadow-black/30 sm:h-16 sm:gap-3 sm:px-4">
      <button
        className={`grid h-10 w-10 place-items-center rounded-xl border border-[#263246] bg-[#111827] text-[#94a3b8] transition hover:border-[#3b82f6] hover:text-white ${
          menuOpen ? "border-[#3b82f6] text-white shadow-sm" : ""
        }`}
        aria-label="打开工作台菜单"
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((value) => !value)}
      >
        <Menu size={18} />
      </button>
      {menuOpen ? (
        <div className="absolute left-6 top-[58px] z-30 w-72 rounded-2xl border border-[#263246] bg-[#0b1220] p-3 shadow-2xl shadow-black/50">
          <div className="mb-2 px-2 text-[11px] font-semibold text-[#94a3b8]">工作台菜单</div>
          <div className="grid gap-1">
            {WORKSPACE_IDS.map((id) => (
              <button
                key={id}
                className={`flex h-10 items-center gap-3 rounded-xl px-3 text-left text-sm transition ${
                  workspace === id ? "bg-[#1d4ed8] text-white" : "text-[#94a3b8] hover:bg-[#111827] hover:text-[#e5eefb]"
                }`}
                onClick={() => switchWorkspace(id)}
              >
                {workspaceIcon(id)}
                <span className="font-medium">{workspaceLabel(id, platform)}</span>
              </button>
            ))}
          </div>
          <div className="mt-3 border-t border-[#1f2a3d] pt-3">
            <button
              className={`${button} h-10 w-full justify-center`}
              onClick={() => {
                refresh();
                setMenuOpen(false);
              }}
            >
              <RefreshCcw size={13} />
              刷新全部数据
            </button>
            <div className="mt-2 rounded-xl bg-[#111827] px-3 py-2 text-[11px] text-[#94a3b8]">
              本地控制台：127.0.0.1:8090
            </div>
          </div>
        </div>
      ) : null}
      <div className="min-w-0 flex-1 sm:min-w-[180px] sm:flex-none">
        <div className="text-[10px] uppercase tracking-wide text-[#94a3b8] sm:text-[11px]">工作区</div>
        <div className="truncate text-base font-semibold text-[#e5eefb] sm:text-lg">{workspaceLabel(workspace, platform)}</div>
      </div>
      <div className="hidden items-center gap-2 lg:flex">
        <StatusPill label="外壳" value={platformShellLabel(platform?.platform.shell)} />
        <StatusPill label="内核" value={platformCoreLabel(platform?.platform.core)} />
        <StatusPill label="模式" value={executionModeLabel(status?.execution_mode)} />
      </div>
      <div className="ml-auto flex min-w-0 items-center gap-3 text-[11px] text-[#94a3b8]">
        {warning ? <span className="hidden max-w-[440px] truncate rounded-full border border-[#854d0e] bg-[#241806] px-3 py-2 text-[#facc15] sm:inline-flex">{warning}</span> : null}
        <div className="hidden h-10 items-center gap-2 rounded-xl border border-[#263246] bg-[#111827] px-2 md:flex sm:px-3">
          <KeyRound size={13} />
          <span className="hidden whitespace-nowrap sm:inline">{session.user?.label || "账号"}</span>
        </div>
        <span className={`hidden rounded-full border px-3 py-2 sm:inline-flex ${status?.opening_paused ? "border-[#854d0e] bg-[#241806] text-[#facc15]" : "border-[#14532d] bg-[#052e1a] text-[#22c55e]"}`}>
          {status?.opening_paused ? "开仓已暂停" : "允许开仓"}
        </span>
        <button className="hidden h-10 rounded-xl border border-[#263246] bg-[#111827] px-3 text-[#94a3b8] hover:text-white md:inline-flex md:items-center" onClick={logout}>
          退出
        </button>
        <button className={button} onClick={refresh}>
          <RefreshCcw size={13} />
        </button>
      </div>
    </header>
  );
}

function MobileBottomNav({
  platform,
  workspace,
  setWorkspace,
}: {
  platform: PlatformOverview | null;
  workspace: WorkspaceId;
  setWorkspace: (value: WorkspaceId) => void;
}) {
  const items: WorkspaceId[] = ["dashboard", "market", "ai", "data", "execution"];
  return (
    <nav className="fixed inset-x-0 bottom-0 z-40 border-t border-[#1f2a3d] bg-[#08111f]/96 px-2 pb-[max(env(safe-area-inset-bottom),8px)] pt-2 shadow-[0_-18px_44px_rgba(0,0,0,0.45)] backdrop-blur md:hidden">
      <div className="grid grid-cols-5 gap-1">
        {items.map((id) => {
          const active = workspace === id;
          return (
            <button
              key={id}
              type="button"
              className={`grid min-h-12 place-items-center rounded-xl px-1 text-[10px] transition ${
                active ? "bg-[#1d4ed8] text-white shadow-inner shadow-blue-950/30" : "text-[#94a3b8] hover:bg-[#111827] hover:text-[#e5eefb]"
              }`}
              onClick={() => setWorkspace(id)}
            >
              {workspaceIcon(id)}
              <span className="mt-0.5 truncate">{mobileWorkspaceLabel(id, platform)}</span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}

function mobileWorkspaceLabel(id: WorkspaceId, platform: PlatformOverview | null) {
  if (id === "dashboard") return "总览";
  if (id === "market") return "图表";
  if (id === "strategy") return "策略";
  if (id === "ai") return "AI";
  if (id === "data") return "快讯";
  if (id === "execution") return "交易";
  return workspaceLabel(id, platform);
}

function StatusPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-[#263246] bg-[#111827] px-3 py-2">
      <div className="text-[10px] uppercase text-[#94a3b8]">{label}</div>
      <div className={`${mono} max-w-36 truncate text-xs font-semibold text-[#e5eefb]`}>{value}</div>
    </div>
  );
}

function LeftRail({
  workspace,
  symbol,
  setSymbol,
  symbols,
  profile,
  status,
  balance,
  busy,
  message,
  canControl,
  postAction,
}: {
  workspace: WorkspaceId;
  symbol: string;
  setSymbol: (value: string) => void;
  symbols: string[];
  profile: StrategyProfile | undefined;
  status: StatusResponse | null;
  balance: Record<string, unknown> | null;
  busy: boolean;
  message: string;
  canControl: boolean;
  postAction: (path: string, body: Record<string, unknown>) => Promise<void>;
}) {
  const rawBalance = objectPayload(balance?.raw);
  const directUsdt = objectPayload(balance?.USDT);
  const usdt = Object.keys(directUsdt).length ? directUsdt : objectPayload(rawBalance.USDT);
  const isDashboard = workspace === "dashboard";
  return (
    <aside className="flex min-h-0 flex-col gap-2 overflow-auto">
      <Surface title={<><ShieldCheck size={13} /> {isDashboard ? "快速控制" : "策略档案"}</>}>
        <select className={`${input} mb-2 w-full ${mono}`} value={symbol} onChange={(event) => setSymbol(event.target.value)}>
          {symbols.map((item) => (
            <option key={item} value={item}>
              {shortSymbol(item)}
            </option>
          ))}
        </select>
        {isDashboard ? (
          <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">
            <BoundaryLine label="策略" value={profile?.enabled ? "运行中" : "研究中"} />
            <BoundaryLine label="授权" value={profile?.opening_authorized ? "已授权" : "未授权"} />
            <BoundaryLine label="模式" value={executionModeLabel(status?.execution_mode)} />
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            <Metric label="档案" value={profile?.profile_name || "--"} />
            <Metric label="策略" value={profile?.enabled ? "运行中" : "研究中"} tone={profile?.enabled ? "good" : "warn"} />
            <Metric label="授权" value={profile?.opening_authorized ? "已授权" : "未授权"} tone={profile?.opening_authorized ? "good" : "warn"} />
            <Metric label="报告" value={profile?.report_enabled ? "开启" : "关闭"} />
          </div>
        )}
        {canControl ? <div className="mt-2 grid grid-cols-2 gap-1">
          <button className={button} disabled={busy} onClick={() => postAction("/api/control/authorize", { operator_id: "console", symbols: [symbol] })}>
            授权开仓
          </button>
          <button className={button} disabled={busy} onClick={() => postAction("/api/control/pause", { operator_id: "console", symbols: [symbol] })}>
            暂停开仓
          </button>
          <button className={button} disabled={busy} onClick={() => postAction("/api/control/enable-report", { operator_id: "console", symbols: [symbol] })}>
            开启报告
          </button>
          <button className={button} disabled={busy} onClick={() => postAction("/api/control/disable-report", { operator_id: "console", symbols: [symbol] })}>
            关闭报告
          </button>
        </div> : null}
        {message ? <div className="mt-2 rounded-xl border border-[#263246] bg-[#101a2d] p-2 text-[11px] text-[#cbd5e1]">{message}</div> : null}
      </Surface>

      {!isDashboard ? (
        <Surface title={<><Wallet size={13} /> 账户</>}>
          <div className="grid grid-cols-2 gap-2">
            <Metric label="USDT 总额" value={num(balance?.usdt_total ?? balance?.total_usdt ?? usdt.total)} />
            <Metric label="USDT 可用" value={num(balance?.usdt_free ?? balance?.free_usdt ?? usdt.free)} />
            <Metric label="风控上限" value={`${num(status?.risk?.max_total_leverage || 4, 1)}x`} />
            <Metric label="交易模式" value={tradeModeLabel(status?.trade_mode)} />
          </div>
        </Surface>
      ) : null}

      {canControl ? <Surface title={<><Power size={13} /> 执行控制</>}>
        <div className="grid grid-cols-1 gap-1">
          <button className={danger} disabled={busy} onClick={() => postAction("/api/control/close-position", { operator_id: "console", symbol })}>
            平仓 {shortSymbol(symbol)}
          </button>
          <button className={danger} disabled={busy} onClick={() => postAction("/api/control/panic-close", { operator_id: "console", symbols: [] })}>
            紧急全平
          </button>
        </div>
      </Surface> : null}
    </aside>
  );
}

function WorkspaceBody({
  workspace,
  symbol,
  setSymbol,
  symbols,
  timeframe,
  setTimeframe,
  source,
  setSource,
  candles,
  ticker,
  warning,
  runtimeStatus,
  balance,
  followerBalance,
  markets,
  news,
  positions,
  orders,
  accountSlots,
  denseZone,
  riskSummary,
  readiness,
  visibleSlots,
  isAdmin,
  busy,
  postAction,
  decisions,
  platform,
}: {
  workspace: WorkspaceId;
  symbol: string;
  setSymbol: (value: string) => void;
  symbols: string[];
  timeframe: string;
  setTimeframe: (value: string) => void;
  source: string;
  setSource: (value: string) => void;
  candles: Candle[];
  ticker: MarketTickerResponse | null;
  warning: string;
  runtimeStatus: StatusResponse | null;
  balance: Record<string, unknown> | null;
  followerBalance: Record<string, unknown> | null;
  markets: MarketSymbolsResponse;
  news: NewsResponse;
  positions: Array<DbRow>;
  orders: Array<DbRow>;
  accountSlots: ExecutionAccountSlot[];
  denseZone: DbRow<DenseZonePayload> | null;
  riskSummary: Record<string, unknown> | null;
  readiness: SystemReadiness | null;
  visibleSlots: Array<"trend" | "follower" | "range">;
  isAdmin: boolean;
  busy: boolean;
  postAction: (path: string, body: Record<string, unknown>) => Promise<void>;
  decisions: Array<DbRow>;
  platform: PlatformOverview | null;
}) {
  const selectedProfile = platform?.strategy_profiles.find((item) => item.symbol === symbol);
  if (workspace === "strategy") {
    return (
      <StrategyWorkspace
        symbol={symbol}
        setSymbol={setSymbol}
        symbols={symbols}
        platform={platform}
        profile={selectedProfile}
        riskSummary={riskSummary}
        status={runtimeStatus}
        isAdmin={isAdmin}
      />
    );
  }
  if (workspace === "dashboard") {
    return (
      <DashboardWorkspace
        symbol={symbol}
        platform={platform}
        runtimeStatus={runtimeStatus}
        balance={balance}
        positions={positions}
        orders={orders}
        decisions={decisions}
        denseZone={denseZone}
        news={news}
        candles={candles}
        ticker={ticker}
        warning={warning}
        readiness={readiness}
        busy={busy}
        postAction={postAction}
      />
    );
  }
  if (workspace === "ai") {
    return <AiBrainWorkspace symbol={symbol} status={runtimeStatus} platform={platform} profile={selectedProfile} decisions={decisions} />;
  }
  if (workspace === "agent") {
    return <AgentGatewayWorkspace platform={platform} />;
  }
  if (workspace === "execution") {
    return (
      <ExecutionWorkspace
        symbol={symbol}
        platform={platform}
        status={runtimeStatus}
        balance={balance}
        followerBalance={followerBalance}
        positions={positions}
        orders={orders}
        accountSlots={accountSlots}
        visibleSlots={visibleSlots}
        riskSummary={riskSummary}
        isAdmin={isAdmin}
        busy={busy}
        postAction={postAction}
      />
    );
  }
  if (workspace === "data") {
    return (
      <DataWorkspace
        platform={platform}
        status={runtimeStatus}
        markets={markets}
        news={news}
        candles={candles}
        warning={warning}
        source={source}
        timeframe={timeframe}
        balance={balance}
        riskSummary={riskSummary}
        positions={positions}
        orders={orders}
        busy={busy}
        postAction={postAction}
      />
    );
  }
  return (
    <MarketWorkspace
      symbol={symbol}
      setSymbol={setSymbol}
      symbols={symbols}
      timeframe={timeframe}
      setTimeframe={setTimeframe}
      source={source}
      setSource={setSource}
      candles={candles}
      warning={warning}
      profile={selectedProfile}
      orders={orders}
      decisions={decisions}
      denseZone={denseZone}
    />
  );
}

function MarketWorkspace({
  symbol,
  setSymbol,
  symbols,
  timeframe,
  setTimeframe,
  source,
  setSource,
  candles,
  warning,
  profile,
  orders,
  decisions,
  denseZone,
}: {
  symbol: string;
  setSymbol: (value: string) => void;
  symbols: string[];
  timeframe: string;
  setTimeframe: (value: string) => void;
  source: string;
  setSource: (value: string) => void;
  candles: Candle[];
  warning: string;
  profile?: StrategyProfile;
  orders: Array<DbRow>;
  decisions: Array<DbRow>;
  denseZone: DbRow<DenseZonePayload> | null;
}) {
  const latest = candles.at(-1);
  const prev = candles.at(-2);
  const changePct = latest && prev ? ((latest.close - prev.close) / prev.close) * 100 : 0;
  const windowCandles = candles.slice(-240);
  const localHigh = Math.max(...windowCandles.map((item) => item.high), 0);
  const localLow = Math.min(...windowCandles.map((item) => item.low).filter(Number.isFinite));
  const volume = latest?.volume || 0;
  const params = profile?.params || {};
  const timeframes = CHART_TIMEFRAMES;
  return (
    <section className="min-h-0 space-y-4 overflow-auto pr-1">
      <Surface
        title={<><BarChart3 size={13} /> 专业行情图表</>}
        action={
          <div className="flex flex-wrap gap-2">
            <select className={`${input} ${mono}`} value={symbol} onChange={(event) => setSymbol(event.target.value)}>
              {symbols.map((item) => (
                <option key={item} value={item}>
                  {shortSymbol(item)}
                </option>
              ))}
            </select>
            <select className={`${input} ${mono}`} value={timeframe} onChange={(event) => setTimeframe(event.target.value)}>
              {timeframes.map((item) => (
                <option key={item} value={item}>
                  {chartTimeframeLabel(item)}
                </option>
              ))}
            </select>
            <select className={`${input} ${mono}`} value={source} onChange={(event) => setSource(event.target.value)}>
              {["binance", "okx", "gateio", "auto"].map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </div>
        }
      >
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
          <Metric label="最新价" value={num(latest?.close)} tone={changePct >= 0 ? "good" : "bad"} />
          <Metric label="涨跌幅" value={pct(changePct)} tone={changePct >= 0 ? "good" : "bad"} />
          <Metric label="最高" value={num(latest?.high)} />
          <Metric label="最低" value={num(latest?.low)} />
          <Metric label="成交量" value={num(volume, 2)} />
          <Metric label="K线数量" value={num(candles.length, 0)} />
        </div>
        <div className="mt-4 flex flex-wrap gap-2 pb-1 text-[11px] text-[#94a3b8]">
          <ChartChip label="KC" value={`${params.kc_length || 20}/${params.kc_scalar || 2.8}`} />
          <ChartChip label="ATR" value={String(params.atr_length || 14)} />
          <ChartChip label="VOL" value={String(params.volume_multiple || "--")} />
          <ChartChip label="KDJ" value={String(params.kdj_length || "--")} />
          <ChartChip label="240根高低" value={`${num(localLow)} - ${num(localHigh)}`} />
          {warning ? <span className="rounded-full border border-[#854d0e] bg-[#241806] px-3 py-1 text-[#facc15]">{warning}</span> : null}
        </div>
        <div className="mt-3">
          <MarketChart
            candles={candles}
            profile={profile}
            orders={orders}
            decisions={decisions}
            denseZone={denseZone?.payload}
            height={700}
            timeframe={timeframe}
            timeframeOptions={timeframes}
            onTimeframeChange={setTimeframe}
          />
        </div>
        <div className="mt-3 flex flex-wrap gap-3 pb-1 text-[11px] text-[#94a3b8]">
          <span><span className="text-[#22c55e]">■</span> 阳线 / 成交量</span>
          <span><span className="text-[#fb7185]">■</span> 阴线 / 成交量</span>
          <span><span className="text-[#e5e7eb]">━</span> EMA 过滤线</span>
          <span><span className="text-[#2454ff]">━</span> 肯特纳上下轨</span>
          <span><span className="text-[#64748b]">━</span> 肯特纳中轨</span>
        </div>
      </Surface>
      <DenseZonePanel denseZone={denseZone?.payload} />
      <Surface title={<><ShieldCheck size={13} /> 图表交易契约</>}>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Metric label="数据源" value={source} />
          <Metric label="周期" value={timeframe} />
          <Metric label="策略档案" value={profile?.profile_name || "--"} />
          <Metric label="状态" value={profile?.enabled ? "运行中" : "研究中"} tone={profile?.enabled ? "good" : "warn"} />
        </div>
      </Surface>
    </section>
  );
}

function ChartChip({ label, value }: { label: string; value: string }) {
  return (
    <span className="shrink-0 whitespace-nowrap rounded-full border border-[#263246] bg-[#101a2d] px-3 py-1 text-[#cbd5e1]">
      {label}: <span className={mono}>{value}</span>
    </span>
  );
}

function chartTimeframeLabel(value: string) {
  const labels: Record<string, string> = {
    "15m": "15分钟",
    "1h": "1小时",
    "4h": "4小时",
    "1d": "1日",
    "1w": "周线",
    "1M": "月线",
  };
  return labels[value] || value;
}

function DenseZonePanel({ denseZone }: { denseZone?: DenseZonePayload }) {
  if (!denseZone) {
    return (
      <Surface title={<><ShieldCheck size={13} /> 密集区结构</>}>
        <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">
          暂无密集区记录。等待下一次交易循环或 AI 扫描后，图表会自动叠加 POC、上下沿和相邻密集区。
        </div>
      </Surface>
    );
  }
  return (
    <Surface title={<><ShieldCheck size={13} /> 密集区结构</>}>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        <Metric label="上沿" value={num(denseZone.zone_high ?? denseZone.vah)} />
        <Metric label="POC/中位" value={num(denseZone.zone_mid ?? denseZone.poc)} />
        <Metric label="下沿" value={num(denseZone.zone_low ?? denseZone.val)} />
        <Metric label="趋势评分" value={confidencePct(denseZone.trend_score)} />
        <Metric label="震荡评分" value={confidencePct(denseZone.range_score)} />
        <Metric label="强度" value={confidencePct(denseZone.strength)} />
      </div>
      <div className="mt-3 grid grid-cols-1 gap-2 text-xs lg:grid-cols-3">
        <BoundaryLine label="当前位置" value={denseZoneLabel(denseZone.current_position)} />
        <BoundaryLine label="突破状态" value={breakoutStatusLabel(denseZone.breakout_status)} />
        <BoundaryLine label="结构描述" value={String(denseZone.structure_label || "等待确认")} />
      </div>
    </Surface>
  );
}

function StrategyWorkspace({
  symbol,
  setSymbol,
  symbols,
  platform,
  profile,
  riskSummary,
  status,
  isAdmin,
}: {
  symbol: string;
  setSymbol: (value: string) => void;
  symbols: string[];
  platform: PlatformOverview | null;
  profile: StrategyProfile | undefined;
  riskSummary: Record<string, unknown> | null;
  status: StatusResponse | null;
  isAdmin: boolean;
}) {
  return (
    <section className="min-h-0 space-y-5 overflow-auto pr-1">
      <div className="grid grid-cols-1 gap-5 2xl:grid-cols-[340px_minmax(0,1fr)]">
        <StrategyListPanel
          symbol={symbol}
          setSymbol={setSymbol}
          profiles={platform?.strategy_profiles || []}
          symbols={symbols}
        />
        <Surface title={<><ShieldCheck size={13} /> 策略参数与执行契约</>}>
          <StrategyParameterGrid profile={profile} />
          <div className="mt-4 grid grid-cols-1 gap-3 xl:grid-cols-2">
            <ContractCard
              title="入场逻辑"
              items={[
                ["信号K线", contractValueLabel(profile?.execution_contract?.signal_candle || "closed_1h_bar")],
                ["成交假设", contractValueLabel(profile?.execution_contract?.entry_fill || "next_tradeable_open")],
                ["反手规则", String(profile?.params?.use_reversal ?? true) === "true" ? "允许先平后反手" : "禁止反手"],
              ]}
            />
            <ContractCard
              title="退出与风控"
              items={[
                ["止损", contractValueLabel(profile?.execution_contract?.stop_rule || "fixed_atr_stop_from_entry")],
                ["中轨退出", contractValueLabel(profile?.execution_contract?.exit_rule || "reverse_cross_of_keltner_midline")],
                ["名义仓位", `${num(profile?.backtest_defaults?.notional_multiple, 2)}x`],
              ]}
            />
          </div>
        </Surface>
      </div>
      <StrategyParameterEditor symbol={symbol} profile={profile} riskSummary={riskSummary} status={status} isAdmin={isAdmin} />
      <BacktestPanel symbol={symbol} setSymbol={setSymbol} symbols={symbols} profile={profile} />
    </section>
  );
}

function StrategyListPanel({
  symbol,
  setSymbol,
  profiles,
  symbols,
}: {
  symbol: string;
  setSymbol: (value: string) => void;
  profiles: StrategyProfile[];
  symbols: string[];
}) {
  const profileSymbols = profiles.length ? profiles.map((item) => item.symbol) : symbols;
  return (
    <Surface title={<><Bot size={13} /> 策略列表</>}>
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-3 2xl:grid-cols-1">
        {profileSymbols.map((item) => {
          const profile = profiles.find((profile) => profile.symbol === item);
          const active = item === symbol;
          return (
            <button
              key={item}
              className={`w-full rounded-2xl border p-4 text-left transition ${
                active
                  ? "border-[#60a5fa] bg-[#102a5c] shadow-[0_12px_30px_rgba(37,99,235,0.22)]"
                  : "border-[#263246] bg-[#101a2d] hover:border-[#3b82f6]"
              }`}
              onClick={() => setSymbol(item)}
            >
              <div className="flex items-center justify-between">
                <span className="rounded-lg border border-[#263246] bg-[#0b1220] px-2 py-1 text-[11px] font-semibold text-[#93c5fd] shadow-sm">
                  Gate.io
                </span>
                <span className={`rounded-full px-2 py-1 text-[11px] ${profile?.enabled ? "bg-[#052e1a] text-[#22c55e]" : "bg-[#241806] text-[#facc15]"}`}>
                  {profile?.enabled ? "运行中" : "研究中"}
                </span>
              </div>
              <div className="mt-3 text-lg font-semibold text-[#e5eefb]">{shortSymbol(item)}</div>
              <div className="mt-2 flex items-center justify-between gap-2 text-[11px] text-[#94a3b8]">
                <span className="min-w-0 truncate">{profile?.profile_name || "待配置档案"}</span>
                <span className="shrink-0">{profile?.opening_authorized ? "已授权" : "未授权"}</span>
              </div>
            </button>
          );
        })}
      </div>
    </Surface>
  );
}

function StrategyParameterGrid({ profile }: { profile?: StrategyProfile }) {
  const params = profile?.params || {};
  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      <Metric label="档案" value={profile?.profile_name || "--"} />
      <Metric label="周期" value={String(profile?.backtest_defaults?.timeframe || "1h")} />
      <Metric label="KC中轨" value={String(params.kc_length || 20)} />
      <Metric label="KC宽度" value={num(params.kc_scalar ?? 2.8, 2)} />
      <Metric label="ATR周期" value={String(params.atr_length || 14)} />
      <Metric label="ATR止损" value={num(params.atr_stop_multiple ?? 1.5, 2)} />
      <Metric label="成交量过滤" value={String(params.use_volume_filter ?? true) === "true" ? "开启" : "关闭"} />
      <Metric label="成交量倍数" value={num(params.volume_multiple ?? 2.5, 2)} />
      <Metric label="动量过滤" value={String(params.momentum_filter || "KDJ")} />
      <Metric label="KDJ周期" value={String(params.kdj_length || 9)} />
    </div>
  );
}

function ContractCard({ title, items }: { title: string; items: Array<[string, string]> }) {
  return (
    <div className="rounded-2xl border border-[#263246] bg-[#101a2d] p-4">
      <div className="font-semibold text-[#e5eefb]">{title}</div>
      <div className="mt-3 grid gap-2 text-xs">
        {items.map(([label, value]) => (
          <BoundaryLine key={label} label={label} value={value} />
        ))}
      </div>
    </div>
  );
}

function contractValueLabel(value: unknown) {
  const text = String(value || "--");
  const labels: Record<string, string> = {
    closed_1h_bar: "1小时收盘确认",
    next_tradeable_open: "下一根可交易开盘价",
    fixed_atr_stop_from_entry: "开仓价固定ATR止损",
    reverse_cross_of_keltner_midline: "反向穿越KC中轨",
    keltner_breakout_with_enabled_filters: "肯特纳突破并通过启用过滤器",
    blocked: "禁止",
  };
  return labels[text] || text;
}

const EDITABLE_STRATEGY_PARAMS = [
  { key: "kc_length", path: "strategy.trend.kc_length", label: "KC中轨周期", step: "1", min: 5, max: 100, scope: "symbol" },
  { key: "kc_scalar", path: "strategy.trend.kc_scalar", label: "KC通道宽度", step: "0.1", min: 0.5, max: 8, scope: "symbol" },
  { key: "atr_length", path: "strategy.trend.atr_length", label: "ATR周期", step: "1", min: 5, max: 100, scope: "symbol" },
  { key: "atr_stop_multiple", path: "strategy.trend.atr_stop_multiple", label: "ATR止损倍数", step: "0.1", min: 0.2, max: 20, scope: "symbol" },
  { key: "vma_length", path: "strategy.trend.vma_length", label: "成交量均线周期", step: "1", min: 5, max: 100, scope: "symbol" },
  { key: "volume_multiple", path: "strategy.trend.volume_multiple", label: "成交量放大倍数", step: "0.1", min: 0.5, max: 8, scope: "symbol" },
  { key: "max_total_leverage", path: "risk.max_total_leverage", label: "全局杠杆硬上限", step: "0.1", min: 0.5, max: 20, scope: "global" },
] as const;

function StrategyParameterEditor({
  symbol,
  profile,
  riskSummary,
  status,
  isAdmin,
}: {
  symbol: string;
  profile?: StrategyProfile;
  riskSummary: Record<string, unknown> | null;
  status: StatusResponse | null;
  isAdmin: boolean;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [proposals, setProposals] = useState<Array<DbRow>>([]);
  const [submitting, setSubmitting] = useState("");
  const [message, setMessage] = useState("");
  const params = profile?.params || {};
  const editableParams = isAdmin ? EDITABLE_STRATEGY_PARAMS : [];

  const loadProposals = useCallback(async () => {
    try {
      const response = await api<ApiList>("/api/proposals?status=pending&limit=30", { timeoutMs: 8000, retries: 1 });
      setProposals((response.items || []).filter((row) => row.payload?.type === "parameter_update"));
    } catch (error) {
      setMessage(`参数提案加载失败：${errText(error)}`);
    }
  }, []);

  useEffect(() => {
    const next: Record<string, string> = {};
    EDITABLE_STRATEGY_PARAMS.forEach((item) => {
      const current = item.scope === "global"
        ? riskSummary?.max_total_leverage ?? status?.risk?.max_total_leverage ?? 4
        : params[item.key];
      next[item.key] = String(current ?? "");
    });
    setValues(next);
    setMessage("");
  }, [profile?.symbol, profile?.profile_name, riskSummary?.max_total_leverage, status?.risk?.max_total_leverage]);

  useEffect(() => {
    void loadProposals();
  }, [loadProposals]);

  const createProposal = async (item: (typeof EDITABLE_STRATEGY_PARAMS)[number]) => {
    const value = Number(values[item.key]);
    if (!Number.isFinite(value)) {
      setMessage(`${item.label} 不是有效数字。`);
      return;
    }
    setSubmitting(item.key);
    setMessage("");
    try {
      const result = await api<Record<string, unknown>>("/api/proposals/parameter", {
        method: "POST",
        timeoutMs: 12000,
        body: JSON.stringify({
          operator_id: "console",
          path: item.path,
          value,
          symbols: item.scope === "global" ? [] : [symbol],
        }),
      });
      setMessage(`已创建 ${item.label} 参数提案 #${String(result.proposal_id || "--")}，审批后生效。`);
      await loadProposals();
    } catch (error) {
      setMessage(errText(error));
    } finally {
      setSubmitting("");
    }
  };

  const handleProposal = async (proposalId: number, action: "approve" | "reject") => {
    setSubmitting(`${action}:${proposalId}`);
    setMessage("");
    try {
      await api<Record<string, unknown>>(`/api/proposals/${proposalId}/${action}`, {
        method: "POST",
        timeoutMs: 12000,
        body: JSON.stringify({ operator_id: "console" }),
      });
      setMessage(action === "approve" ? `提案 #${proposalId} 已审批生效。` : `提案 #${proposalId} 已拒绝，未修改运行参数。`);
      await loadProposals();
    } catch (error) {
      setMessage(errText(error));
    } finally {
      setSubmitting("");
    }
  };

  return (
    <Surface title={<><ShieldCheck size={13} /> 策略参数修改</>}>
      <div className="mb-3 rounded-xl border border-[#854d0e] bg-[#241806] p-3 text-[11px] leading-relaxed text-[#facc15]">
        {isAdmin
          ? "修改不会直接写入实盘配置，只会创建待审批提案；审批通过后才热加载到策略运行参数。普通账户只能在交易执行页修改自己账户的杠杆上限。"
          : "当前账号只有查看权限。策略参数由管理员维护；你只能在交易执行页修改自己账户的杠杆上限。"}
      </div>
      {editableParams.length ? <div className="grid grid-cols-1 gap-3 xl:grid-cols-2 2xl:grid-cols-4">
        {editableParams.map((item) => {
          const current = item.scope === "global"
            ? riskSummary?.max_total_leverage ?? status?.risk?.max_total_leverage ?? 4
            : params[item.key];
          const changed = String(current ?? "") !== String(values[item.key] ?? "");
          return (
            <div key={item.key} className="rounded-2xl border border-[#263246] bg-[#101a2d] p-3">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-xs font-semibold text-[#e5eefb]">{item.label}</div>
                  <div className="mt-1 text-[10px] text-[#7b8798]">
                    当前值 <span className={mono}>{num(current, 4)}</span> / 范围 {item.min}-{item.max}
                    {item.scope === "global" ? " / 全局硬风控" : " / 当前标的"}
                  </div>
                </div>
                <span className={`rounded-full px-2 py-1 text-[10px] ${changed ? "bg-[#241806] text-[#facc15]" : "bg-[#052e1a] text-[#22c55e]"}`}>
                  {changed ? "待提交" : "未改动"}
                </span>
              </div>
              <div className="mt-3 grid grid-cols-[minmax(0,1fr)_92px] gap-2">
                <input
                  className={`${input} ${mono} w-full`}
                  type="number"
                  min={item.min}
                  max={item.max}
                  step={item.step}
                  value={values[item.key] ?? ""}
                  onChange={(event) => setValues((prev) => ({ ...prev, [item.key]: event.target.value }))}
                />
                <button className={button} disabled={!changed || submitting === item.key} onClick={() => createProposal(item)}>
                  提交提案
                </button>
              </div>
            </div>
          );
        })}
      </div> : null}
      {isAdmin ? <div className="mt-4 overflow-hidden rounded-2xl border border-[#263246] bg-[#0b1220]">
        <div className="flex h-10 items-center justify-between border-b border-[#e6edf5] px-3 text-xs">
          <span className="font-semibold text-[#e5eefb]">待审批参数提案</span>
          <button className={button} onClick={() => void loadProposals()}>刷新提案</button>
        </div>
        <div className="max-h-64 overflow-auto">
          {proposals.length ? (
            proposals.map((row) => (
              <ParameterProposalRow
                key={row.id}
                row={row}
                busy={submitting.endsWith(`:${row.id}`)}
                onApprove={() => void handleProposal(row.id, "approve")}
                onReject={() => void handleProposal(row.id, "reject")}
              />
            ))
          ) : (
            <div className="px-3 py-6 text-center text-xs text-[#7b8798]">暂无待审批参数提案。</div>
          )}
        </div>
      </div> : null}
      {message ? <div className="mt-3 rounded-xl border border-[#263246] bg-[#0b1220] p-3 text-xs text-[#94a3b8]">{message}</div> : null}
    </Surface>
  );
}

function ParameterProposalRow({
  row,
  busy,
  onApprove,
  onReject,
}: {
  row: DbRow;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  const payload = row.payload || {};
  const changes = (payload.changes as Record<string, { old?: unknown; new?: unknown }> | undefined) || {};
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_160px] gap-3 border-b border-[#1f2a3d] p-3 text-xs">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="rounded-full bg-[#241806] px-2 py-1 text-[10px] text-[#facc15]">待审批</span>
          <span className={mono}>#{row.id}</span>
          <span className="truncate text-[#7b8798]">{row.created_at}</span>
        </div>
        <div className="mt-2 grid gap-1">
          {Object.entries(changes).map(([path, change]) => (
            <div key={path} className="rounded-lg bg-[#101a2d] px-2 py-1">
              <span className="font-semibold text-[#e5eefb]">{paramPathLabel(path)}</span>
              <span className={`${mono} ml-2 text-[#94a3b8]`}>{num(change.old, 4)} → {num(change.new, 4)}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="grid content-center gap-2">
        <button className={button} disabled={busy} onClick={onApprove}>审批生效</button>
        <button className={danger} disabled={busy} onClick={onReject}>拒绝</button>
      </div>
    </div>
  );
}

function paramPathLabel(path: string) {
  const shortKey = path.split(".").at(-1) || path;
  const labels: Record<string, string> = {
    kc_length: "KC中轨周期",
    kc_scalar: "KC通道宽度",
    atr_length: "ATR周期",
    atr_stop_multiple: "ATR止损倍数",
    vma_length: "成交量均线周期",
    volume_multiple: "成交量放大倍数",
    ema_length: "EMA过滤周期",
    max_total_leverage: "全局杠杆硬上限",
  };
  return labels[shortKey] || shortKey;
}

function DashboardWorkspace({
  symbol,
  platform,
  runtimeStatus,
  balance,
  positions,
  orders,
  decisions,
  denseZone,
  news,
  candles,
  ticker,
  warning,
  readiness,
  busy,
  postAction,
}: {
  symbol: string;
  platform: PlatformOverview | null;
  runtimeStatus: StatusResponse | null;
  balance: Record<string, unknown> | null;
  positions: Array<DbRow>;
  orders: Array<DbRow>;
  decisions: Array<DbRow>;
  denseZone: DbRow<DenseZonePayload> | null;
  news: NewsResponse;
  candles: Candle[];
  ticker: MarketTickerResponse | null;
  warning: string;
  readiness: SystemReadiness | null;
  busy: boolean;
  postAction: (path: string, body: Record<string, unknown>) => Promise<void>;
}) {
  const profile = platform?.strategy_profiles.find((item) => item.symbol === symbol);
  const position = positionSnapshot(positions, symbol);
  const latestDecision = (runtimeStatus?.latest_decisions?.[symbol]?.payload || decisions[0]?.payload || { state: "等待下一次AI判断" }) as Record<string, unknown>;
  const latestDecisionParts = decisionParts(latestDecision);
  const latestCandle = candles.at(-1);
  const account = accountSnapshot(balance);
  const newsItems = visibleNewsItems(news).slice(0, 8);
  const newsWarnings = (news.warnings || []).filter((item) => !isInternalNewsText(item));
  const readinessOverall = readiness?.overall || "warn";
  const blockedChecks = (readiness?.checks || []).filter((check) => check.status === "block");
  const warnChecks = (readiness?.checks || []).filter((check) => check.status === "warn");
  const exchangePayload = readiness?.exchange_safety?.payload || {};
  const reconciliationPayload = readiness?.latest_reconciliation?.payload || {};
  const dataHealthPayload = readiness?.latest_data_health?.payload || {};
  const orderLifecyclePayload = readiness?.latest_order_lifecycle?.payload || {};
  const mode = runtimeStatus?.execution_mode || platform?.platform.execution_mode || readiness?.execution_mode || "mock";
  const openingAuthorized = runtimeStatus?.enabled_symbols?.includes(symbol) || Boolean(profile?.opening_authorized);
  const liveReady = profile?.live_ready || readiness?.overall === "ok";
  const aiReady = readiness?.deepseek_ready || Boolean(runtimeStatus?.ai?.enabled);
  const newsAge = news.age_minutes != null ? `${num(news.age_minutes, 1)} 分钟` : "等待刷新";
  const realtimePrice = numberValue(ticker?.last, ticker?.mark);
  const latestPrice = realtimePrice != null ? num(realtimePrice) : latestCandle ? num(latestCandle.close) : "--";
  const latestPriceLabel = realtimePrice != null ? "实时价" : "K线收盘";
  return (
    <section className="min-h-0 space-y-3 overflow-auto sm:space-y-4 sm:pr-1">
      <div className="rounded-2xl border border-[#263246] bg-[#0b1220] p-3 shadow-[0_18px_44px_rgba(0,0,0,0.30)] sm:p-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2 text-[12px] font-semibold text-[#60a5fa]">
              <ServerCog size={14} />
              AI 量化实盘指挥台
            </div>
            <div className="mt-2 flex flex-wrap items-end gap-x-4 gap-y-2">
              <h1 className="text-xl font-semibold tracking-tight text-[#f8fafc] sm:text-2xl">{shortSymbol(symbol)} 趋势策略</h1>
              <span className={`${mono} rounded-full border border-[#263246] bg-[#101a2d] px-3 py-1 text-xs text-[#cbd5e1]`}>
                {latestPriceLabel} {latestPrice}
              </span>
              {realtimePrice != null && latestCandle ? (
                <span className="rounded-full border border-[#263246] bg-[#101a2d] px-3 py-1 text-xs text-[#94a3b8]">
                  K线收盘 {num(latestCandle.close)}
                </span>
              ) : null}
              <LiveOpsBadge readiness={readinessOverall} mode={mode} />
            </div>
          </div>
          <div className="grid w-full grid-cols-2 gap-2 sm:grid-cols-4 xl:min-w-[520px] xl:w-auto">
            <DashboardStatusPill label="开仓授权" value={openingAuthorized ? "已授权" : "未授权"} tone={openingAuthorized ? "good" : "bad"} />
            <DashboardStatusPill label="交易所" value={exchangePayload.can_open_new_entries ? "可开仓" : "禁止开仓"} tone={exchangePayload.can_open_new_entries ? "good" : "bad"} />
            <DashboardStatusPill label="DeepSeek" value={aiReady ? "已配置" : "降级"} tone={aiReady ? "good" : "warn"} />
            <DashboardStatusPill label="新闻缓存" value={newsAge} tone={newsWarnings.length ? "warn" : "good"} />
          </div>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-2 sm:gap-3 lg:grid-cols-3 2xl:grid-cols-6">
          <HeroMetric label="持仓状态" value={position ? positionSideLabel(position.side) : "空仓"} tone={position ? "warn" : "good"} />
          <HeroMetric label="浮动盈亏" value={position ? `${num(position.pnl)} USDT` : "--"} tone={pnlTone(position?.pnl)} />
          <HeroMetric label="AI动作" value={dashboardActionLabel(latestDecision)} />
          <HeroMetric label="仓位档" value={dashboardTierLabel(latestDecision)} />
          <HeroMetric label="对账状态" value={reconciliationStatusLabel(reconciliationPayload.status)} tone={reconciliationPayload.status === "ok" ? "good" : "warn"} />
          <HeroMetric label="K线数据" value={warning || `${num(candles.length, 0)} 根`} tone={warning ? "warn" : "good"} />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:gap-4 2xl:grid-cols-[360px_minmax(520px,1fr)_360px]">
        <div className="grid min-w-0 gap-4">
          <DashboardPanel title={<><Wallet size={14} /> 账户与持仓</>}>
            <div className="rounded-xl bg-[#0f172a] p-4 text-white">
              <div className="flex items-center justify-between gap-2">
                <div className="text-[11px] text-[#cbd5e1]">USDT 权益</div>
                <span className={`rounded-full px-2 py-1 text-[10px] ${account.ok === false ? "bg-[#3b1117] text-[#fb7185]" : account.source === "gate_live_readonly" || account.source === "live" ? "bg-[#052e1a] text-[#22c55e]" : "bg-[#241806] text-[#facc15]"}`}>
                  {account.sourceLabel}
                </span>
              </div>
              <div className={`${mono} mt-2 text-3xl font-semibold`}>{num(account.total)}</div>
              {account.message ? <div className="mt-2 rounded-lg border border-[#263246] bg-[#07111f] px-3 py-2 text-[11px] text-[#94a3b8]">{account.message}</div> : null}
              <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                <PlainKV label="可用" value={`${num(account.free)} USDT`} />
                <PlainKV label="已用" value={`${num(account.used)} USDT`} />
                <PlainKV label="风控上限" value={`${num(runtimeStatus?.risk?.max_total_leverage || 4, 1)}x`} />
                <PlainKV label="余额来源" value={account.sourceLabel} />
              </div>
            </div>
            <div className="mt-3 flex items-center justify-between gap-3 rounded-xl border border-[#263246] bg-[#101a2d] p-3">
              <div>
                <div className="text-[11px] text-[#94a3b8]">标的 / 方向</div>
                <div className="mt-1 text-xl font-semibold text-[#e5eefb]">
                  {position ? `${shortSymbol(symbol)} ${positionSideLabel(position.side)}` : `${shortSymbol(symbol)} 空仓`}
                </div>
              </div>
              <span className={`rounded-full px-3 py-1 text-[11px] font-medium ${position ? "bg-[#241806] text-[#facc15]" : "bg-[#052e1a] text-[#22c55e]"}`}>
                {position ? "持仓中" : "无敞口"}
              </span>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <HeroMetric label="数量" value={position ? num(position.qty, 6) : "--"} />
              <HeroMetric label="开仓价" value={position ? num(position.entryPrice) : "--"} />
              <HeroMetric label="标记价" value={position ? num(position.markPrice ?? latestCandle?.close) : "--"} />
              <HeroMetric label="名义价值" value={position ? `${num(position.notional)} USDT` : "--"} />
              <HeroMetric label="浮盈亏" value={position ? `${num(position.pnl)} USDT` : "--"} tone={pnlTone(position?.pnl)} />
              <HeroMetric label="止损价" value={position?.stopLoss != null ? num(position.stopLoss) : "--"} />
            </div>
            <div className="mt-3 rounded-xl border border-[#263246] bg-[#101a2d] p-3">
              <BoundaryLine label="密集区位置" value={denseZoneLabel(decisionValue(latestDecisionParts, ["dense_zone_position", "current_position"]))} />
              <BoundaryLine label="最近订单" value={orders[0] ? orderLifecycleSummary(orders[0].payload) : "暂无订单"} />
            </div>
          </DashboardPanel>
        </div>

        <div className="grid min-w-0 gap-4">
          <DashboardPanel title={<><BrainCircuit size={14} /> AI 决策与仓位</>} action={<span className="rounded-full border border-[#1d4ed8] bg-[#102a5c] px-3 py-1 text-[11px] text-[#bfdbfe]">只缩放 / 否决</span>}>
            <DecisionSummary data={latestDecision} />
            <DecisionNarrative data={latestDecision} />
          </DashboardPanel>

          <DashboardPanel title={<><ShieldCheck size={14} /> 实盘安全闸</>} action={<span className={`rounded-full px-3 py-1 text-[11px] ${readinessToneClass(readinessOverall)}`}>{readinessLabel(readinessOverall)}</span>}>
            <div className="grid gap-2 md:grid-cols-3">
              <HealthMini label="交易所对账" value={reconciliationStatusLabel(reconciliationPayload.status)} ok={reconciliationPayload.status === "ok"} />
              <HealthMini label="数据新鲜度" value={healthStatusLabel(dataHealthPayload.status)} ok={dataHealthPayload.status === "ok"} />
              <HealthMini label="实盘准备" value={liveReady ? "就绪" : "待复核"} ok={Boolean(liveReady)} />
            </div>
            <div className="mt-3 grid gap-2">
              {blockedChecks.length || warnChecks.length ? (
                [...blockedChecks, ...warnChecks].slice(0, 4).map((check) => (
                  <div key={check.id} className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs">
                    <div className="flex items-center justify-between gap-3">
                      <span className="font-semibold text-[#e5eefb]">{readinessCheckLabel(check.label)}</span>
                      <span className={`rounded-full px-2 py-1 text-[10px] ${readinessToneClass(check.status)}`}>{readinessLabel(check.status)}</span>
                    </div>
                    <div className="mt-2 leading-relaxed text-[#94a3b8]">{readinessDetail(check.detail)}</div>
                  </div>
                ))
              ) : (
                <div className="rounded-xl border border-[#14532d] bg-[#052e1a] p-3 text-xs text-[#22c55e]">当前没有阻断项。</div>
              )}
            </div>
          </DashboardPanel>
        </div>

        <div className="grid min-w-0 gap-4">
          <DashboardPanel
            title={<><Newspaper size={14} /> 新闻快讯</>}
            action={<button className={button} disabled={busy} onClick={() => postAction("/api/news/refresh", { operator_id: "console" })}>刷新</button>}
          >
            {newsWarnings.length ? (
              <div className="mb-3 rounded-xl border border-[#854d0e] bg-[#241806] p-3 text-xs text-[#facc15]">{newsWarnings.join("; ")}</div>
            ) : null}
            <div className="grid max-h-[500px] gap-2 overflow-auto pr-1">
              {newsItems.length ? newsItems.slice(0, 7).map((item, idx) => <DashboardNewsItem key={idx} item={item} />) : (
                <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">暂无新闻快讯。</div>
              )}
            </div>
          </DashboardPanel>
        </div>
      </div>

      <details className="rounded-2xl border border-[#263246] bg-[#0b1220] p-4 text-xs text-[#94a3b8] shadow-[0_18px_44px_rgba(0,0,0,0.28)]">
        <summary className="cursor-pointer font-semibold text-[#e5eefb]">展开完整生产就绪检查、执行链路与原始审计</summary>
        <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
          <ReadinessPanel readiness={readiness} />
          <DashboardPanel title={<><Power size={14} /> 执行链路</>}>
            <div className="mb-3 rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs">
              <BoundaryLine label="最近订单状态" value={orderLifecycleSummary(orderLifecyclePayload)} />
              <BoundaryLine label="新开仓状态" value={exchangePayload.can_open_new_entries ? "允许" : "禁止"} />
              <BoundaryLine label="人工处理" value={exchangeSafetyReason(exchangePayload.manual_action)} />
            </div>
            <div className="grid gap-2">
              {orders.length ? orders.slice(0, 4).map((row) => <CompactOrderCard key={`${row.id}-${row.created_at}`} row={row} />) : (
                <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">暂无订单记录。</div>
              )}
            </div>
          </DashboardPanel>
        </div>
      </details>
    </section>
  );
}

function DashboardPanel({ title, action, children }: { title: ReactNode; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="min-w-0 rounded-2xl border border-[#263246] bg-[#0b1220] shadow-[0_18px_44px_rgba(0,0,0,0.28)]">
      <div className="flex min-h-12 items-center justify-between gap-3 border-b border-[#1f2a3d] px-4 py-3 text-sm font-semibold text-[#e5eefb]">
        <div className="flex items-center gap-2">{title}</div>
        {action}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function DashboardStatusPill({ label, value, tone = "default" }: { label: string; value: string; tone?: "default" | "good" | "bad" | "warn" }) {
  const toneClass =
    tone === "good" ? "border-[#14532d] bg-[#052e1a] text-[#22c55e]" :
    tone === "bad" ? "border-[#7f1d1d] bg-[#2a0f14] text-[#fb7185]" :
    tone === "warn" ? "border-[#854d0e] bg-[#241806] text-[#facc15]" :
    "border-[#263246] bg-[#101a2d] text-[#cbd5e1]";
  return (
    <div className={`rounded-xl border px-3 py-2 ${toneClass}`}>
      <div className="text-[10px] opacity-80">{label}</div>
      <div className={`${mono} mt-1 truncate text-xs font-semibold`}>{value}</div>
    </div>
  );
}

function HeroMetric({ label, value, tone = "default" }: { label: string; value: string; tone?: "default" | "good" | "bad" | "warn" }) {
  const toneClass =
    tone === "good" ? "text-[#22c55e]" : tone === "bad" ? "text-[#fb7185]" : tone === "warn" ? "text-[#facc15]" : "text-[#e5eefb]";
  return (
    <div className="min-w-0 rounded-xl border border-[#263246] bg-[#101a2d] p-3">
      <div className="text-[11px] text-[#94a3b8]">{label}</div>
      <div className={`${mono} mt-1 truncate text-sm font-semibold ${toneClass}`}>{value}</div>
    </div>
  );
}

function PlainKV({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-[#1e293b] bg-[#0f172a] px-3 py-2">
      <div className="text-[10px] text-[#cbd5e1]">{label}</div>
      <div className={`${mono} mt-1 truncate font-semibold text-white`}>{value}</div>
    </div>
  );
}

function HealthMini({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return (
    <div className={`rounded-xl border p-3 text-xs ${ok ? "border-[#14532d] bg-[#052e1a] text-[#22c55e]" : "border-[#854d0e] bg-[#241806] text-[#facc15]"}`}>
      <div className="text-[10px] opacity-80">{label}</div>
      <div className={`${mono} mt-1 font-semibold`}>{value}</div>
    </div>
  );
}

function LiveOpsBadge({ readiness, mode }: { readiness: string; mode: string }) {
  const tone = readiness === "ok" ? "bg-[#052e1a] text-[#22c55e]" : readiness === "block" ? "bg-[#2a0f14] text-[#fb7185]" : "bg-[#241806] text-[#facc15]";
  return <span className={`rounded-full px-3 py-1 text-[11px] ${tone}`}>{executionModeLabel(mode)} / {readinessLabel(readiness)}</span>;
}

function DashboardNewsItem({ item }: { item: Record<string, unknown> }) {
  const title = cleanNewsText(item.title || item.headline || item.summary || "--");
  const source = cleanNewsText(item.source || "新闻源");
  return (
    <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs">
      <div className="line-clamp-3 font-medium leading-5 text-[#e5eefb]">{title}</div>
      <div className="mt-2 flex items-center justify-between gap-3 text-[11px] text-[#94a3b8]">
        <span>{source}</span>
        <span className={mono}>{String(item.published_at || item.time || "")}</span>
      </div>
    </div>
  );
}

function visibleNewsItems(news: NewsResponse): Array<Record<string, unknown>> {
  const candidates: Array<Record<string, unknown>> = [];
  const pushRecord = (value: unknown) => {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      candidates.push(value as Record<string, unknown>);
    }
  };
  const pushPayload = (value: unknown) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return;
    const record = value as Record<string, unknown>;
    const nestedTimeline = Array.isArray(record.timeline) ? record.timeline : [];
    const nestedItems = Array.isArray(record.items) ? record.items : [];
    if (nestedTimeline.length || nestedItems.length) {
      [...nestedTimeline, ...nestedItems].forEach(pushRecord);
      return;
    }
    pushRecord(record);
  };

  (news.timeline || []).forEach(pushPayload);
  (news.items || []).forEach((row) => pushPayload(row.payload));
  const seen = new Set<string>();
  return candidates
    .map(normalizeNewsItem)
    .filter((item) => !isInternalNewsItem(item))
    .filter((item) => {
      const key = `${item.published_at || item.time || ""}:${item.title || item.headline || item.summary || ""}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

function normalizeNewsItem(item: Record<string, unknown>): Record<string, unknown> {
  return {
    ...item,
    title: cleanNewsText(item.title || item.headline || item.summary || ""),
    headline: cleanNewsText(item.headline || item.title || item.summary || ""),
    summary: cleanNewsText(item.summary || item.title || item.headline || ""),
    source: cleanNewsText(item.source || "新闻源"),
  };
}

function cleanNewsText(value: unknown) {
  const text = String(value || "");
  if (!looksLikeMojibake(text)) return text;
  try {
    const bytes = Uint8Array.from(Array.from(text, (char) => char.charCodeAt(0) & 0xff));
    const decoded = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    return decoded.length > text.length * 0.5 ? decoded : text;
  } catch {
    return text;
  }
}

function looksLikeMojibake(text: string) {
  return /[ãÃÂâ][\u0080-\u00ff]?|é[\u0080-\u00ff]|å[\u0080-\u00ff]|ä[\u0080-\u00ff]|ç[\u0080-\u00ff]/.test(text);
}

function isInternalNewsItem(item: Record<string, unknown>) {
  const text = cleanNewsText(item.title || item.headline || item.summary || item.message || "").toLowerCase();
  return isInternalNewsText(text);
}

function isInternalNewsText(value: unknown) {
  const text = String(value || "").toLowerCase();
  return [
    "daily_news_flash_context_attached",
    "news_context_48h_attached",
    "rss_error",
    "readtimeout",
    "httperror",
    "vip超大",
    "抵扣1000",
    "交易权益",
    "速成手册",
  ].some((needle) => text.includes(needle));
}

function CompactOrderCard({ row }: { row: DbRow }) {
  const payload = row.payload || {};
  const side = String(payload.side || payload.action || "--");
  const status = String(payload.status || "--");
  return (
    <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs">
      <div className="flex items-center justify-between gap-3">
        <span className="font-semibold text-[#e5eefb]">{shortSymbol(String(row.symbol || payload.symbol || "--"))}</span>
        <span className={mono}>{row.created_at}</span>
      </div>
      <div className="mt-2 grid grid-cols-4 gap-2">
        <Metric label="方向" value={sideLabel(side)} />
        <Metric label="数量" value={num(payload.amount ?? payload.qty, 6)} />
        <Metric label="价格" value={num(payload.price ?? payload.average)} />
        <Metric label="状态" value={orderStatusLabel(status)} />
      </div>
    </div>
  );
}

type PositionSnapshot = {
  side: string;
  qty: number | null;
  entryPrice: number | null;
  markPrice: number | null;
  pnl: number | null;
  notional: number | null;
  stopLoss: number | null;
};

function positionSnapshot(positions: Array<DbRow>, symbol: string): PositionSnapshot | null {
  const row = positions.find((item) => String(item.symbol || item.payload?.symbol || "") === symbol) || positions[0];
  if (!row) return null;
  const payload = row.payload || {};
  const raw = objectPayload(payload.raw);
  const info = objectPayload(raw.info);
  const qty = numberValue(payload.qty, payload.amount, payload.contracts, payload.size, raw.contracts, raw.size, info.size);
  if (qty != null && Math.abs(qty) === 0) return null;
  const side = String(payload.side || raw.side || (qty != null && qty < 0 ? "short" : qty != null && qty > 0 ? "long" : "--"));
  const entryPrice = numberValue(payload.entry_price, payload.entryPrice, raw.entryPrice, raw.entry_price, info.entry_price);
  const markPrice = numberValue(payload.mark_price, payload.markPrice, raw.markPrice, raw.mark_price, info.mark_price);
  const pnl = numberValue(payload.unrealized_pnl, payload.unrealised_pnl, raw.unrealizedPnl, raw.unrealized_pnl, raw.unrealised_pnl, info.unrealised_pnl);
  const notional = numberValue(payload.notional, payload.contract_value, raw.notional, raw.contract_value, qty != null && markPrice != null ? Math.abs(qty * markPrice) : null);
  const stopLoss = numberValue(payload.stop_loss, payload.stopLoss, payload.sl_price, raw.stop_loss, raw.stopLoss, info.stop_loss);
  return { side, qty, entryPrice, markPrice, pnl, notional, stopLoss };
}

function accountSnapshot(balance: Record<string, unknown> | null) {
  const payload = balance || {};
  const raw = objectPayload(payload.raw);
  const rawUsdt = objectPayload(raw.USDT);
  const total = numberValue(payload.usdt_total, payload.total_usdt, rawUsdt.total, objectPayload(payload.total).USDT);
  const free = numberValue(payload.usdt_free, payload.free_usdt, rawUsdt.free, objectPayload(payload.free).USDT);
  const used = numberValue(payload.usdt_used, payload.used_usdt, rawUsdt.used, objectPayload(payload.used).USDT);
  const source = String(payload.balance_source || payload.mode || "unknown");
  const sourceLabel =
    payload.ok === false
      ? "读取失败"
      : source === "gate_live_readonly"
      ? "Gate只读"
      : source === "live"
        ? "Gate实盘"
        : source === "mock"
          ? "模拟余额"
          : source === "live_readonly_failed"
            ? "读取失败"
            : "--";
  const message = typeof payload.message === "string" ? payload.message : "";
  return { total, free, used, source, sourceLabel, ok: payload.ok, message };
}

function objectPayload(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

type DecisionParts = {
  root: Record<string, unknown>;
  ai: Record<string, unknown>;
  risk: Record<string, unknown>;
  signal: Record<string, unknown>;
  technical: Record<string, unknown>;
  event: Record<string, unknown>;
};

function decisionParts(data: Record<string, unknown>): DecisionParts {
  const nestedPayload = objectPayload(data.payload);
  const root = Object.keys(nestedPayload).length ? nestedPayload : objectPayload(data);
  const signal = objectPayload(root.signal);
  return {
    root,
    ai: objectPayload(root.ai),
    risk: objectPayload(root.risk),
    signal,
    technical: objectPayload(signal.technical_evidence),
    event: objectPayload(root.event || data.event),
  };
}

function decisionValue(parts: DecisionParts, keys: string[], extraSources: Array<Record<string, unknown>> = []): unknown {
  const sources = [parts.ai, parts.risk, parts.root, parts.signal, parts.technical, parts.event, ...extraSources];
  for (const key of keys) {
    for (const source of sources) {
      const value = source[key];
      if (value !== undefined && value !== null && value !== "") return value;
    }
  }
  return undefined;
}

function decisionAction(parts: DecisionParts): unknown {
  return decisionValue(parts, ["action_suggestion", "veto_action", "action", "state"]);
}

function decisionReason(parts: DecisionParts): string {
  return String(
    decisionValue(parts, ["brief_reason", "reason", "summary", "major_news_title", "title"]) ||
      "等待下一次 AI 判断。"
  );
}

function decisionPatternValue(parts: DecisionParts): unknown {
  const aiPattern = decisionValue(parts, ["pattern_type"]);
  if (!unknownish(aiPattern)) return aiPattern;
  const structuredPattern = decisionValue(parts, ["regime_pattern_name", "regime_pattern_family", "pattern_name", "pattern_family", "pattern_type"]);
  if (!unknownish(structuredPattern)) return structuredPattern;
  return inferPatternFromReason(decisionReason(parts)) || aiPattern || structuredPattern;
}

function unknownish(value: unknown): boolean {
  const text = String(value ?? "").trim().toLowerCase();
  return !text || ["--", "unknown", "none", "null", "undefined", "未知"].includes(text);
}

function inferPatternFromReason(reason: string): string | null {
  const text = reason.toLowerCase();
  if (reason.includes("上升楔形") || text.includes("rising wedge")) return "rising_wedge";
  if (reason.includes("下降楔形") || text.includes("falling wedge")) return "falling_wedge";
  if (reason.includes("收敛三角") || reason.includes("对称三角") || text.includes("symmetrical triangle")) return "symmetrical_triangle";
  if (reason.includes("上升三角") || text.includes("ascending triangle")) return "ascending_triangle";
  if (reason.includes("下降三角") || text.includes("descending triangle")) return "descending_triangle";
  if (reason.includes("箱体") || reason.includes("矩形") || text.includes("rectangle") || text.includes("box range")) return "range_rectangle";
  if (reason.includes("假突破") || text.includes("false breakout")) return "false_breakout";
  if (reason.includes("突破") || text.includes("breakout")) return "breakout";
  return null;
}

type DecisionSizing = {
  label: string;
  scale: string;
  activeTier: string;
  note: string;
};

function decisionSizing(parts: DecisionParts): DecisionSizing {
  const riskTier = parts.risk.position_tier;
  const riskScale = parts.risk.position_scale;
  if (!unknownish(riskTier) || riskScale !== undefined && riskScale !== null) {
    const activeTier = String(riskTier || "block");
    return {
      label: tierLabel(activeTier),
      scale: positionScaleLabel(riskScale),
      activeTier,
      note: "后端 RiskManager 已返回正式仓位档。",
    };
  }

  const action = String(decisionAction(parts) || "").toLowerCase();
  const direction = String(decisionValue(parts, ["direction"]) || "").toLowerCase();
  if (!hasActionableStrategySignal(parts) || ["flat", "neutral", "none", "观望"].includes(direction) || ["hold", "wait", "block"].includes(action)) {
    return {
      label: "阻断",
      scale: "0%",
      activeTier: "block",
      note: "当前没有本地策略入场信号，AI 只做观察、减仓或否决，不展示可开仓仓位。",
    };
  }

  if (["reduce", "veto", "deny"].includes(action)) {
    const confidence = Number(decisionValue(parts, ["confidence"]));
    const activeTier = Number.isFinite(confidence) && confidence >= 0.7 ? "normal" : "weak";
    return {
      label: tierLabel(activeTier),
      scale: activeTier === "normal" ? "50%" : "25%",
      activeTier,
      note: "本地策略有信号，但 AI 建议降档。",
    };
  }

  const scale = aiScaleFromConfidence(decisionValue(parts, ["confidence"]));
  const activeTier = tierKeyFromConfidence(decisionValue(parts, ["confidence"]));
  return {
    label: scale.label,
    scale: scale.scale,
    activeTier,
    note: "本地策略有入场信号，AI 置信度用于五档缩放。",
  };
}

function hasActionableStrategySignal(parts: DecisionParts): boolean {
  const explicitValues = [
    parts.signal.action,
    parts.signal.signal_action,
    parts.signal.strategy_action,
    parts.technical.original_strategy_action,
    parts.technical.strategy_action,
    parts.technical.signal_action,
    parts.root.strategy_action,
    parts.root.signal_action,
  ];
  if (explicitValues.some(isEntrySignalValue)) return true;
  return truthyFlag(parts.technical.long_condition) || truthyFlag(parts.technical.short_condition) || truthyFlag(parts.signal.long_condition) || truthyFlag(parts.signal.short_condition);
}

function isEntrySignalValue(value: unknown): boolean {
  const text = String(value || "").trim().toLowerCase();
  return ["buy", "sell", "long", "short", "open_long", "open_short", "entry_long", "entry_short", "enter_long", "enter_short"].includes(text);
}

function truthyFlag(value: unknown): boolean {
  return value === true || String(value).trim().toLowerCase() === "true";
}

function numberValue(...values: unknown[]): number | null {
  for (const value of values) {
    const number = Number(value);
    if (Number.isFinite(number)) return number;
  }
  return null;
}

function pnlTone(value: unknown): "default" | "good" | "bad" | "warn" {
  const number = Number(value);
  if (!Number.isFinite(number)) return "default";
  return number >= 0 ? "good" : "bad";
}

function positionSideLabel(value: unknown) {
  const text = String(value || "--").toLowerCase();
  if (["long", "buy"].includes(text)) return "多仓";
  if (["short", "sell"].includes(text)) return "空仓";
  return text;
}

function sideLabel(value: unknown) {
  const text = String(value || "--").toLowerCase();
  if (text === "buy") return "买入";
  if (text === "sell") return "卖出";
  if (text === "long") return "做多";
  if (text === "short") return "做空";
  return text;
}

function orderStatusLabel(value: unknown) {
  const text = String(value || "--");
  const labels: Record<string, string> = {
    open: "挂单中",
    closed: "已成交",
    filled: "已成交",
    partially_filled: "部分成交",
    submitted: "已提交",
    accepted: "已接收",
    intent_recorded: "意图已记录",
    submitting: "提交中",
    cancel_pending: "撤单中",
    canceled: "已撤销",
    cancelled: "已撤销",
    cancel_failed: "撤单失败",
    rejected: "拒单",
    blocked: "阻断",
    unknown: "未知",
  };
  return labels[text] || text;
}

function dashboardActionLabel(data: Record<string, unknown>) {
  return actionLabel(decisionAction(decisionParts(data)));
}

function dashboardTierLabel(data: Record<string, unknown>) {
  const parts = decisionParts(data);
  const sizing = decisionSizing(parts);
  return `${sizing.label} / ${sizing.scale}`;
}

function ReadinessPanel({ readiness }: { readiness: SystemReadiness | null }) {
  const overall = readiness?.overall || "warn";
  const exchangePayload = readiness?.exchange_safety?.payload || {};
  const reconciliationPayload = readiness?.latest_reconciliation?.payload || {};
  const orderLifecyclePayload = readiness?.latest_order_lifecycle?.payload || {};
  const dataHealthPayload = readiness?.latest_data_health?.payload || {};
  const aiDriftPayload = readiness?.latest_ai_drift?.payload || {};
  const newsRiskPayload = readiness?.latest_news_risk_review?.payload || {};
  const workerHeartbeats = readiness?.latest_worker_heartbeats || {};
  const workerHeartbeatDetails = readiness?.worker_heartbeat_details || [];
  const blockedReasons = (readiness?.checks || []).filter((check) => check.status === "block").map((check) => readinessCheckLabel(check.label));
  return (
    <Surface
      title={<><ShieldCheck size={13} /> 策略与 AI 运行就绪检查</>}
      action={<span className={`rounded-full px-3 py-1 text-[11px] ${readinessToneClass(overall)}`}>{readinessLabel(overall)}</span>}
    >
      <div className="grid grid-cols-5 gap-3">
        <Metric label="整体状态" value={readinessLabel(overall)} tone={overall === "ok" ? "good" : overall === "block" ? "bad" : "warn"} />
        <Metric label="执行模式" value={executionModeLabel(readiness?.execution_mode)} />
        <Metric label="交易模式" value={tradeModeLabel(readiness?.trade_mode)} />
        <Metric label="授权档案" value={`${num(readiness?.authorized_profile_count, 0)}/${num(readiness?.profile_count, 0)}`} />
        <Metric label="DeepSeek" value={readiness?.deepseek_ready ? "已配置" : "缺失"} tone={readiness?.deepseek_ready ? "good" : "warn"} />
      </div>
      <div className="mt-3 grid grid-cols-5 gap-3">
        <Metric label="交易所状态" value={exchangeStatusLabel(exchangePayload.status)} tone={exchangePayload.can_open_new_entries ? "good" : "bad"} />
        <Metric label="新开仓" value={exchangePayload.can_open_new_entries ? "允许" : "禁止"} tone={exchangePayload.can_open_new_entries ? "good" : "bad"} />
        <Metric label="最近对账" value={reconciliationStatusLabel(reconciliationPayload.status)} tone={reconciliationPayload.status === "ok" ? "good" : "warn"} />
        <Metric label="数据新鲜度" value={healthStatusLabel(dataHealthPayload.status)} tone={dataHealthPayload.status === "ok" ? "good" : dataHealthPayload.status === "block" ? "bad" : "warn"} />
        <Metric label="AI 漂移" value={healthStatusLabel(aiDriftPayload.status)} tone={aiDriftPayload.status === "ok" ? "good" : aiDriftPayload.status === "block" ? "bad" : "warn"} />
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 text-[11px] text-[#94a3b8]">
        <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3">
          <div className="font-semibold text-[#e5eefb]">交易所降级说明</div>
          <div className="mt-1 leading-relaxed">{exchangeSafetyReason(exchangePayload.reason)}</div>
          <div className="mt-1 leading-relaxed">{exchangeSafetyReason(exchangePayload.manual_action)}</div>
        </div>
        <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3">
          <div className="font-semibold text-[#e5eefb]">订单状态机</div>
          <div className="mt-1 leading-relaxed">{orderLifecycleSummary(orderLifecyclePayload)}</div>
          <div className="mt-1 leading-relaxed">{workerHeartbeatSummary(workerHeartbeats)}</div>
          <div className="mt-1 leading-relaxed">阻断：{blockedReasons.length ? blockedReasons.join(" / ") : "无"}</div>
        </div>
        <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3">
          <div className="font-semibold text-[#e5eefb]">最新新闻风险审计</div>
          <div className="mt-1 leading-relaxed">{newsRiskSummary(newsRiskPayload)}</div>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-4">
        {(workerHeartbeatDetails.length ? workerHeartbeatDetails : fallbackWorkerHeartbeatDetails(workerHeartbeats)).map((worker) => (
          <div key={worker.worker} className="rounded-xl border border-[#263246] bg-[#0b1220] p-3 text-xs">
            <div className="flex items-center justify-between gap-2">
              <span className="font-semibold text-[#e5eefb]">{workerLabel(worker.worker)}</span>
              <span className={`rounded-full px-2 py-1 text-[10px] ${workerToneClass(worker.status)}`}>{workerStatusLabel(worker.status)}</span>
            </div>
            <div className="mt-2 grid gap-1 text-[11px] text-[#94a3b8]">
              <BoundaryLine label="最后心跳" value={worker.checked_at ? formatTime(worker.checked_at) : "--"} />
              <BoundaryLine label="年龄" value={heartbeatAgeLabel(worker.age_seconds, worker.allowed_seconds)} />
              <BoundaryLine label="原因" value={workerReasonLabel(worker.reason)} />
            </div>
          </div>
        ))}
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2">
        {(readiness?.checks || []).map((check) => (
          <div key={check.id} className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs">
            <div className="flex items-center justify-between gap-2">
              <span className="font-semibold text-[#e5eefb]">{readinessCheckLabel(check.label)}</span>
              <span className={`rounded-full px-2 py-1 text-[10px] ${readinessToneClass(check.status)}`}>{readinessLabel(check.status)}</span>
            </div>
            <div className="mt-2 text-[11px] leading-relaxed text-[#94a3b8]">{readinessDetail(check.detail)}</div>
          </div>
        ))}
      </div>
    </Surface>
  );
}

function exchangeStatusLabel(status: unknown) {
  const value = String(status || "--");
  const labels: Record<string, string> = {
    ok: "正常",
    degraded_readonly: "只读降级",
    reconciliation_required: "需要对账",
    blocked: "阻断",
  };
  return labels[value] || value;
}

function reconciliationStatusLabel(status: unknown) {
  if (!status) return "未完成";
  return exchangeStatusLabel(status);
}

function healthStatusLabel(status: unknown) {
  const value = String(status || "warn");
  if (value === "ok") return "正常";
  if (value === "block") return "阻断";
  return "警告";
}

function exchangeSafetyReason(value: unknown) {
  const text = String(value || "Mock 模式无需私有对账；实盘模式必须先完成 Gate 持仓、余额、挂单与止损对账。");
  return text
    .replace("No manual action required.", "当前无需人工处理。")
    .replace("no_manual_action_required", "当前无需人工处理。")
    .replace("exchange_reconciliation_not_run", "尚未完成交易所私有状态对账。")
    .replace("exchange_private_state_stale_over_5m", "交易所私有状态超过 5 分钟不可验证，禁止新开仓。")
    .replace("exchange_reconciliation_required", "交易所状态与本地状态需要人工复核。")
    .replace("mock_gateway_no_private_reconciliation_required", "Mock 网关不需要私有交易所对账。")
    .replace("exchange_reconciliation_ok", "交易所私有状态对账通过。");
}

function newsRiskSummary(payload: Record<string, unknown>) {
  if (!Object.keys(payload).length) return "当前没有重大新闻复评记录。";
  const event = payload.event && typeof payload.event === "object" ? (payload.event as Record<string, unknown>) : {};
  const risk = payload.risk && typeof payload.risk === "object" ? (payload.risk as Record<string, unknown>) : {};
  const title = String(event.title || "重大新闻复评");
  const reason = String(risk.reason || "--");
  return `${title}；风控结论：${reason}`;
}

function orderLifecycleSummary(payload: Record<string, unknown>) {
  if (!Object.keys(payload).length) return "无记录";
  const status = String(payload.status || "--");
  const clientOrderId = String(payload.client_order_id || "").slice(-8);
  const labels: Record<string, string> = {
    intent_recorded: "意图已记录",
    submitting: "提交中",
    submitted: "已提交",
    accepted: "已接收",
    partially_filled: "部分成交",
    filled: "已成交",
    cancel_pending: "撤单中",
    cancelled: "已撤单",
    cancel_failed: "撤单失败",
    rejected: "拒单",
    unknown: "未知",
    duplicate_suppressed: "重复抑制",
    blocked: "阻断",
  };
  return `${labels[status] || status}${clientOrderId ? ` #${clientOrderId}` : ""}`;
}

function workerHeartbeatSummary(rows: Record<string, DbRow | null>) {
  const entries = Object.entries(rows);
  if (!entries.length) return "Worker 心跳：无记录";
  const failed = entries
    .filter(([, row]) => !row || String(row.payload?.status || "warn") !== "ok")
    .map(([worker]) => worker.replace("_worker", ""));
  return failed.length ? `Worker 心跳异常：${failed.join(" / ")}` : "Worker 心跳正常";
}

function fallbackWorkerHeartbeatDetails(rows: Record<string, DbRow | null>): WorkerHeartbeatDetail[] {
  return Object.entries(rows).map(([worker, row]) => {
    const payload = row?.payload || {};
    return {
      worker,
      status: String(payload.status || (row ? "warn" : "missing")),
      reason: String(payload.reason || (row ? "heartbeat_unstructured" : "heartbeat_missing")),
      checked_at: typeof payload.checked_at === "string" ? payload.checked_at : row?.created_at || null,
      last_success_at: typeof payload.last_success_at === "string" ? payload.last_success_at : null,
      age_seconds: null,
      allowed_seconds: null,
      row_id: row?.id ?? null,
    };
  });
}

function workerLabel(worker: string) {
  const labels: Record<string, string> = {
    trading_worker: "交易循环",
    news_worker: "新闻刷新",
    price_monitor_worker: "价格监控",
    order_status_worker: "订单轮询",
  };
  return labels[worker] || worker.replace(/_/g, " ");
}

function workerStatusLabel(status: unknown) {
  const value = String(status || "warn");
  const labels: Record<string, string> = {
    ok: "正常",
    warn: "警告",
    block: "失败",
    stale: "过期",
    missing: "缺失",
  };
  return labels[value] || value;
}

function workerToneClass(status: unknown) {
  const value = String(status || "warn");
  if (value === "ok") return "bg-[#052e1a] text-[#22c55e]";
  if (value === "block" || value === "missing" || value === "stale") return "bg-[#2a0f14] text-[#fb7185]";
  return "bg-[#241806] text-[#facc15]";
}

function heartbeatAgeLabel(ageSeconds?: number | null, allowedSeconds?: number | null) {
  if (ageSeconds == null || !Number.isFinite(ageSeconds)) return "--";
  const age = ageSeconds < 60 ? `${Math.max(0, Math.round(ageSeconds))}秒` : `${Math.round(ageSeconds / 60)}分钟`;
  if (allowedSeconds == null || !Number.isFinite(allowedSeconds)) return age;
  const limit = allowedSeconds < 60 ? `${Math.round(allowedSeconds)}秒` : `${Math.round(allowedSeconds / 60)}分钟`;
  return `${age} / 上限 ${limit}`;
}

function workerReasonLabel(reason: unknown) {
  const text = String(reason || "--");
  const labels: Record<string, string> = {
    worker_ok: "运行正常",
    cycle_ok: "交易循环完成",
    news_refresh_ok: "新闻刷新完成",
    price_monitor_ok: "价格监控完成",
    order_status_refresh_ok: "订单轮询完成",
    heartbeat_missing: "没有心跳记录",
    heartbeat_stale: "心跳过期",
    heartbeat_unstructured: "旧版心跳记录",
  };
  return labels[text] || text;
}

function formatTime(value: string) {
  const time = new Date(value);
  if (Number.isNaN(time.getTime())) return value;
  return time.toLocaleString("zh-CN", { hour12: false });
}

function readinessLabel(status: string) {
  if (status === "ok") return "正常";
  if (status === "block") return "阻断";
  return "警告";
}

function readinessToneClass(status: string) {
  if (status === "ok") return "border border-[#14532d] bg-[#052e1a] text-[#22c55e]";
  if (status === "block") return "border border-[#7f1d1d] bg-[#2a0f14] text-[#fb7185]";
  return "border border-[#854d0e] bg-[#241806] text-[#facc15]";
}

function readinessCheckLabel(label: string) {
  const labels: Record<string, string> = {
    Database: "数据库",
    "Strategy profiles": "策略档案",
    "Opening authorization": "开仓授权",
    "Opening pause": "开仓暂停",
    DeepSeek: "DeepSeek",
    "Risk limits": "风控上限",
    "News cache": "新闻缓存",
    "Backtest audit": "回测审计",
    "Exchange safety": "交易所安全",
    "Exchange reconciliation": "交易所对账",
    "Data freshness": "数据新鲜度",
    "AI drift": "AI 漂移",
    "Major news risk review": "重大新闻复评",
    "Order lifecycle": "订单生命周期",
    "Worker heartbeat": "Worker 心跳",
    "Runtime maintenance": "运行维护",
    "Live AI guard": "实盘 AI 保护",
  };
  return labels[label] || label;
}

function readinessDetail(detail: string) {
  return detail
    .replace("SQLite store is reachable.", "SQLite 存储可访问。")
    .replace("profiles enabled.", "个策略档案已启用。")
    .replace("symbols authorized for opening.", "个标的已授权开仓。")
    .replace("Opening is paused.", "开仓当前处于暂停状态。")
    .replace("Opening is enabled.", "开仓当前允许。")
    .replace("DeepSeek API key is configured.", "DeepSeek API Key 已配置。")
    .replace("DeepSeek API key is missing; AI decisions will degrade.", "DeepSeek API Key 缺失，AI 决策会降级。")
    .replace("Max total leverage:", "总杠杆上限：")
    .replace("Latest news cache is missing.", "新闻缓存缺失。")
    .replace("Latest backtest run is missing.", "回测记录缺失。")
    .replace("Latest exchange reconciliation is missing.", "交易所对账记录缺失。")
    .replace("Latest data freshness check is missing.", "数据新鲜度记录缺失。")
    .replace("Latest AI drift check is missing.", "AI 漂移记录缺失。")
    .replace("Latest major news risk review is missing.", "重大新闻复评记录缺失。")
    .replace("Latest order lifecycle event is missing.", "订单生命周期记录缺失。")
    .replace("All runtime worker heartbeats are fresh.", "所有运行 worker 心跳新鲜。")
    .replace("Worker heartbeat problem:", "Worker 心跳异常：")
    .replace("Runtime maintenance has not run yet.", "运行维护尚未执行。")
    .replace("Runtime maintenance completed without warnings.", "运行维护无告警完成。")
    .replace("Runtime maintenance warnings:", "运行维护告警：")
    .replace("Disk space is below the configured floor.", "磁盘空间低于配置下限。")
    .replace("Live mode requires a configured AI key for the current policy.", "当前策略要求实盘模式必须配置 AI Key。")
    .replace("Latest news cache was updated", "新闻缓存更新于")
    .replace("Latest backtest run was updated", "最近回测更新于")
    .replace("Latest exchange reconciliation was updated", "交易所对账更新于")
    .replace("Latest data freshness check was updated", "数据新鲜度更新于")
    .replace("Latest AI drift check was updated", "AI 漂移更新于")
    .replace("Latest major news risk review was updated", "重大新闻复评更新于")
    .replace("Latest order lifecycle event was updated", "订单生命周期更新于")
    .replace("less than 1 minute ago.", "1 分钟内。")
    .replace("minutes ago.", "分钟前。");
}
function RoadmapItem({ title, body, done = false }: { title: string; body: string; done?: boolean }) {
  return (
    <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3">
      <div className={done ? "text-[#22c55e]" : "text-[#facc15]"}>{title}</div>
      <div className="mt-1">{body}</div>
    </div>
  );
}

function BacktestPanel({
  symbol,
  setSymbol,
  symbols,
  profile,
}: {
  symbol: string;
  setSymbol: (value: string) => void;
  symbols: string[];
  profile: StrategyProfile | undefined;
}) {
  const [startDate, setStartDate] = useState("2022-01-01");
  const [endDate, setEndDate] = useState("2026-05-20");
  const [dataSource, setDataSource] = useState("binance");
  const [feeRate, setFeeRate] = useState("0.0004");
  const [slippageBps, setSlippageBps] = useState("0");
  const [leverage, setLeverage] = useState("4");
  const [fundingRate, setFundingRate] = useState("0");
  const [minOrderQty, setMinOrderQty] = useState("0");
  const [maxParticipation, setMaxParticipation] = useState("1");
  const [aiProxy, setAiProxy] = useState(false);
  const [job, setJob] = useState<BacktestJob | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [optimization, setOptimization] = useState<OptimizationResult | null>(null);
  const [runs, setRuns] = useState<Array<DbRow>>([]);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const trades = result?.trade_ledger || result?.trades || [];

  const loadRuns = useCallback(async () => {
    try {
      const response = await api<ApiList>(`/api/backtest/runs?limit=24&symbol=${encodeURIComponent(symbol)}`, {
        timeoutMs: 8000,
        retries: 1,
      });
      setRuns(response.items || []);
    } catch (error) {
      setError(`回测历史加载失败：${errText(error)}`);
    }
  }, [symbol]);

  useEffect(() => {
    const defaults = profile?.backtest_defaults || {};
    setDataSource(String(defaults.data_source || "binance"));
    setFeeRate(String(defaults.fee_rate ?? "0.0004"));
    setSlippageBps(String(defaults.slippage_bps ?? "0"));
    setLeverage(String(defaults.leverage ?? "4"));
    setFundingRate(String(defaults.funding_rate_per_8h ?? "0"));
    setMinOrderQty(String(defaults.min_order_qty ?? "0"));
    setMaxParticipation(String(defaults.max_volume_participation ?? "1"));
  }, [profile?.symbol]);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  const poll = async (jobId: string) => {
    for (let idx = 0; idx < 480; idx += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1200));
      const next = await api<BacktestJob>(`/api/backtest/jobs/${jobId}`, { timeoutMs: 8000, retries: 1 });
      setJob(next);
      if (next.status === "completed") return next;
      if (next.status === "failed") throw new Error(next.error || "job failed");
    }
    throw new Error("job timeout");
  };

  const basePayload = () => ({
    operator_id: "console",
    symbol,
    timeframe: "1h",
    limit: 50000,
    data_source: dataSource,
    start_date: startDate,
    end_date: endDate,
    initial_equity: 200,
    fee_rate: Number(feeRate) || 0,
    slippage_bps: Number(slippageBps) || 0,
    leverage: Number(leverage) || 1,
    funding_rate_per_8h: Number(fundingRate) || 0,
    min_order_qty: Number(minOrderQty) || 0,
    max_volume_participation: Math.max(0.01, Math.min(1, Number(maxParticipation) || 1)),
  });

  const runBacktest = async () => {
    setRunning(true);
    setError("");
    setResult(null);
    setOptimization(null);
    try {
      const started = await api<{ job_id: string }>("/api/backtest/trend/job", {
        method: "POST",
        body: JSON.stringify({ ...basePayload(), ai_proxy: aiProxy }),
        timeoutMs: 12000,
      });
      const done = await poll(started.job_id);
      setResult(done.result as BacktestResult);
      await loadRuns();
    } catch (error) {
      setError(errText(error));
    } finally {
      setRunning(false);
    }
  };

  const optimize = async () => {
    setRunning(true);
    setError("");
    setResult(null);
    setOptimization(null);
    try {
      const opt = profile?.optimization_defaults || {};
      const started = await api<{ job_id: string }>("/api/backtest/trend/optimize/job", {
        method: "POST",
        timeoutMs: 12000,
        body: JSON.stringify({
          ...basePayload(),
          validation_ratio: Number(opt.validation_ratio) || 0.3,
          min_trades: Number(opt.min_trades) || 20,
          max_candidates: Number(opt.max_candidates) || 512,
          top_n: Number(opt.top_n) || 10,
          ema_lengths: arrayValue<number>(opt.ema_lengths, [89]),
          kc_lengths: arrayValue<number>(opt.kc_lengths, [20]),
          kc_scalars: arrayValue<number>(opt.kc_scalars, [2.4, 2.6, 2.8, 3.0, 3.2]),
          atr_lengths: arrayValue<number>(opt.atr_lengths, [14]),
          vma_lengths: arrayValue<number>(opt.vma_lengths, [20]),
          volume_multiples: arrayValue<number>(opt.volume_multiples, [2.0, 2.2, 2.5, 2.8, 3.0]),
          atr_stop_multiples: arrayValue<number>(opt.atr_stop_multiples, [1.2, 1.5, 1.8, 2.0]),
          position_fractions: arrayValue<number>(opt.position_fractions, [Number(profile?.params?.position_fraction) || 0.5]),
          use_ema_filters: arrayValue<boolean>(opt.use_ema_filters, [false]),
          use_volume_filters: arrayValue<boolean>(opt.use_volume_filters, [true]),
          momentum_filters: arrayValue<string>(opt.momentum_filters, ["kdj"]),
          kdj_lengths: arrayValue<number>(opt.kdj_lengths, [7, 9, 14]),
        }),
      });
      const done = await poll(started.job_id);
      setOptimization(done.result as OptimizationResult);
      await loadRuns();
    } catch (error) {
      setError(errText(error));
    } finally {
      setRunning(false);
    }
  };

  const applyRun = (row: DbRow) => {
    const payload = row.payload as Record<string, unknown>;
    const request = (payload.request as Record<string, unknown> | undefined) || {};
    const runType = String(payload.type || "");
    if (typeof request.symbol === "string" && symbols.includes(request.symbol)) setSymbol(request.symbol);
    if (typeof request.data_source === "string") setDataSource(request.data_source);
    if (typeof request.start_date === "string") setStartDate(request.start_date);
    if (typeof request.end_date === "string") setEndDate(request.end_date);
    if (request.fee_rate !== undefined) setFeeRate(String(request.fee_rate));
    if (request.slippage_bps !== undefined) setSlippageBps(String(request.slippage_bps));
    if (request.leverage !== undefined) setLeverage(String(request.leverage));
    if (request.funding_rate_per_8h !== undefined) setFundingRate(String(request.funding_rate_per_8h));
    if (request.min_order_qty !== undefined) setMinOrderQty(String(request.min_order_qty));
    if (request.max_volume_participation !== undefined) setMaxParticipation(String(request.max_volume_participation));
    setAiProxy(Boolean(request.ai_proxy));
    if (runType === "parameter_optimization") {
      setOptimization(payload.result as OptimizationResult);
      setResult(null);
    } else {
      setResult(payload.result as BacktestResult);
      setOptimization(null);
    }
  };

  const exportCurrentTrades = () => {
    if (!trades.length) return;
    exportTradesCsv(trades, `${shortSymbol(symbol)}_${startDate}_${endDate}_trades.csv`);
  };

  return (
    <Surface
      title={<><LineChart size={13} /> 策略回测与寻优</>}
      action={<span className={mono}>{job?.progress || 0}%</span>}
    >
      <div className="grid gap-3 2xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="rounded-2xl border border-[#263246] bg-[#101a2d] p-3">
          <div className="mb-3 flex items-center justify-between">
            <div>
              <div className="text-xs font-semibold text-[#e5eefb]">回测设置</div>
              <div className="mt-1 text-[11px] text-[#7b8798]">信号使用已收盘K线，成交按下一可交易价格；实盘与回测必须共享同一策略契约。</div>
            </div>
            <span className="rounded-full bg-[#0b1220] px-3 py-1 text-[11px] text-[#94a3b8] shadow-sm">
              档案 <span className={mono}>{profile?.profile_name || "--"}</span>
            </span>
          </div>
          <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
            <BacktestField label="标的">
              <select className={`${input} ${mono} w-full`} value={symbol} onChange={(event) => setSymbol(event.target.value)}>
                {symbols.map((item) => (
                  <option key={item} value={item}>{shortSymbol(item)}</option>
                ))}
              </select>
            </BacktestField>
            <BacktestField label="数据源">
              <select className={`${input} ${mono} w-full`} value={dataSource} onChange={(event) => setDataSource(event.target.value)}>
                {["binance", "okx", "gateio", "auto"].map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </BacktestField>
            <BacktestField label="开始日期">
              <input className={`${input} ${mono} w-full`} value={startDate} onChange={(event) => setStartDate(event.target.value)} />
            </BacktestField>
            <BacktestField label="结束日期">
              <input className={`${input} ${mono} w-full`} value={endDate} onChange={(event) => setEndDate(event.target.value)} />
            </BacktestField>
            <BacktestField label="手续费率">
              <input className={`${input} ${mono} w-full`} value={feeRate} onChange={(event) => setFeeRate(event.target.value)} />
            </BacktestField>
            <BacktestField label="滑点 bps">
              <input className={`${input} ${mono} w-full`} value={slippageBps} onChange={(event) => setSlippageBps(event.target.value)} />
            </BacktestField>
            <BacktestField label="杠杆">
              <input className={`${input} ${mono} w-full`} value={leverage} onChange={(event) => setLeverage(event.target.value)} />
            </BacktestField>
            <BacktestField label="资金费/8h">
              <input className={`${input} ${mono} w-full`} value={fundingRate} onChange={(event) => setFundingRate(event.target.value)} />
            </BacktestField>
            <BacktestField label="最小数量">
              <input className={`${input} ${mono} w-full`} value={minOrderQty} onChange={(event) => setMinOrderQty(event.target.value)} />
            </BacktestField>
            <BacktestField label="成交参与">
              <input className={`${input} ${mono} w-full`} value={maxParticipation} onChange={(event) => setMaxParticipation(event.target.value)} />
            </BacktestField>
            <BacktestField label="AI代理过滤">
              <label className="flex h-9 items-center gap-2 rounded-lg border border-[#263246] bg-[#0b1220] px-2 text-xs text-[#94a3b8]">
                <input type="checkbox" checked={aiProxy} onChange={(event) => setAiProxy(event.target.checked)} />
                启用本地代理
              </label>
            </BacktestField>
          </div>
        </div>
        <div className="rounded-2xl border border-[#263246] bg-[#0b1220] p-3">
          <div className="text-xs font-semibold text-[#e5eefb]">执行假设</div>
          <div className="mt-3 grid gap-2 text-[11px]">
            <BoundaryLine label="初始权益" value="200 USDT" />
            <BoundaryLine label="名义仓位" value={`${num(profile?.backtest_defaults?.notional_multiple, 2)}x`} />
            <BoundaryLine label="手续费" value={`${num(Number(feeRate) * 100, 4)}% / side`} />
            <BoundaryLine label="滑点" value={`${num(slippageBps, 2)} bps`} />
            <BoundaryLine label="资金费" value={`${num(Number(fundingRate) * 100, 4)}% / 8h`} />
            <BoundaryLine label="成交参与" value={`${num(Number(maxParticipation) * 100, 2)}% volume`} />
          </div>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <button className={button} disabled={running} onClick={runBacktest}>{running ? "运行中" : "开始回测"}</button>
            <button className={button} disabled={running} onClick={optimize}>参数寻优</button>
            <button className={`${button} col-span-2`} disabled={!trades.length || running} onClick={exportCurrentTrades}>
              导出当前交割单 CSV
            </button>
          </div>
          {job?.message ? <div className="mt-2 text-[11px] text-[#94a3b8]">{job.message}</div> : null}
        </div>
      </div>
      {error ? <div className="mt-2 rounded-xl border border-[#7f1d1d] bg-[#2a0f14] p-2 text-[11px] text-[#fb7185]">{error}</div> : null}
      {result ? <BacktestMetrics result={result} /> : null}
      {optimization ? <OptimizationView result={optimization} /> : null}
      {trades.length ? <TradeLedgerTable trades={trades} /> : null}
      <BacktestRunHistory
        runs={runs}
        onSelect={applyRun}
      />
    </Surface>
  );
}

function BacktestField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] uppercase tracking-wide text-[#7b8798]">{label}</span>
      {children}
    </label>
  );
}

function BacktestMetrics({ result }: { result: BacktestResult }) {
  const raw = result.raw_ai_proxy;
  const aiApplied = Boolean(result.ai_guard_applied || raw);
  return (
    <div className="mt-3 space-y-3">
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-6">
        <Metric label="收益率" value={pct(result.total_return_pct)} tone={Number(result.total_return_pct) >= 0 ? "good" : "bad"} />
        <Metric label="最大回撤" value={pct(result.max_drawdown_pct)} tone="warn" />
        <Metric label="交易数" value={num(result.trade_count, 0)} />
        <Metric label="胜率" value={pct(result.win_rate_pct)} />
        <Metric label="盈利因子" value={num(result.profit_factor, 3)} tone={Number(result.profit_factor) >= 1 ? "good" : "bad"} />
        <Metric label="成本占比" value={pct(result.cost_model?.cost_pct_of_initial_equity)} />
        <Metric label="资金费" value={num(result.cost_model?.total_funding_paid, 4)} />
        <Metric label="跳过订单" value={num(result.skipped_orders?.length, 0)} />
      </div>
      {aiApplied ? (
        <div className="rounded-2xl border border-[#854d0e] bg-[#241806] p-3">
          <div className="mb-2 flex items-center justify-between text-xs">
            <span className="font-semibold text-[#facc15]">AI代理对比</span>
            <span className="text-[11px] text-[#facc15]">用于评估过滤/减仓是否正优化，不等同真实 DeepSeek 历史调用</span>
          </div>
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-6">
            <Metric label="原始收益" value={pct(raw?.total_return_pct)} />
            <Metric label="AI后收益" value={pct(result.total_return_pct)} tone={Number(result.total_return_pct) >= Number(raw?.total_return_pct || 0) ? "good" : "bad"} />
            <Metric label="原始回撤" value={pct(raw?.max_drawdown_pct)} />
            <Metric label="AI后回撤" value={pct(result.max_drawdown_pct)} tone={Number(result.max_drawdown_pct) <= Number(raw?.max_drawdown_pct || 0) ? "good" : "warn"} />
            <Metric label="原始交易" value={num(raw?.trade_count, 0)} />
            <Metric label="AI后交易" value={num(result.trade_count, 0)} />
          </div>
        </div>
      ) : null}
    </div>
  );
}

function TradeLedgerTable({ trades }: { trades: BacktestTrade[] }) {
  return (
    <div className="mt-3 overflow-hidden rounded-2xl border border-[#263246] bg-[#0b1220]">
      <div className="flex h-10 items-center justify-between border-b border-[#e6edf5] px-3 text-xs">
        <span className="font-semibold text-[#e5eefb]">交割单明细</span>
        <span className="text-[11px] text-[#7b8798]">显示前 {Math.min(trades.length, 160)} / {trades.length} 笔</span>
      </div>
      <div className="max-h-72 overflow-auto">
        <table className="w-full min-w-[1180px] text-left text-[10px]">
          <thead className="sticky top-0 bg-[#101a2d] text-[#94a3b8]">
            <tr>
              <th className="px-2 py-2">开仓时间</th>
              <th className="px-2 py-2">方向</th>
              <th className="px-2 py-2">开仓价</th>
              <th className="px-2 py-2">数量</th>
              <th className="px-2 py-2">止损价</th>
              <th className="px-2 py-2">平仓时间</th>
              <th className="px-2 py-2">平仓价</th>
              <th className="px-2 py-2">退出</th>
              <th className="px-2 py-2">盈亏</th>
              <th className="px-2 py-2">收益</th>
              <th className="px-2 py-2">费用</th>
              <th className="px-2 py-2">滑点</th>
              <th className="px-2 py-2">资金费</th>
              <th className="px-2 py-2">成交率</th>
              <th className="px-2 py-2">MAE</th>
            </tr>
          </thead>
          <tbody>
            {trades.slice(0, 160).map((trade, idx) => (
              <tr key={`${trade.entry_time || idx}-${trade.exit_time || idx}`} className="border-t border-[#1f2a3d] text-[#2f3b52]">
                <td className={`${mono} px-2 py-2`}>{trade.entry_time || "--"}</td>
                <td className="px-2 py-2">{formatSide(trade.side)}</td>
                <td className={`${mono} px-2 py-2`}>{num(trade.entry_price, 4)}</td>
                <td className={`${mono} px-2 py-2`}>{num(trade.qty, 4)}</td>
                <td className={`${mono} px-2 py-2`}>{num(trade.stop_loss_price, 4)}</td>
                <td className={`${mono} px-2 py-2`}>{trade.exit_time || "--"}</td>
                <td className={`${mono} px-2 py-2`}>{num(trade.exit_price, 4)}</td>
                <td className="px-2 py-2">{trade.exit_reason || "--"}</td>
                <td className={`${mono} px-2 py-2 ${Number(trade.pnl) >= 0 ? "text-[#22c55e]" : "text-[#fb7185]"}`}>{num(trade.pnl, 4)}</td>
                <td className={`${mono} px-2 py-2`}>{pct(trade.return_pct)}</td>
                <td className={`${mono} px-2 py-2`}>{num(trade.fee_paid, 4)}</td>
                <td className={`${mono} px-2 py-2`}>{num(trade.slippage_paid, 4)}</td>
                <td className={`${mono} px-2 py-2`}>{num(trade.funding_paid, 4)}</td>
                <td className={`${mono} px-2 py-2`}>{pct((trade.fill_ratio ?? 1) * 100)}</td>
                <td className={`${mono} px-2 py-2 text-[#fb7185]`}>{pct(trade.max_adverse_excursion_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function formatSide(side?: string) {
  const normalized = String(side || "").toLowerCase();
  if (normalized.includes("long") || normalized === "buy") return "做多";
  if (normalized.includes("short") || normalized === "sell") return "做空";
  return side || "--";
}

function exportTradesCsv(trades: BacktestTrade[], filename: string) {
  const headers = [
    "entry_time",
    "side",
    "entry_price",
    "qty",
    "stop_loss_price",
    "exit_time",
    "exit_price",
    "exit_reason",
    "pnl",
    "return_pct",
    "fee_paid",
    "slippage_paid",
    "funding_paid",
    "requested_qty",
    "filled_qty",
    "fill_ratio",
    "holding_bars",
    "intrabar_path",
    "max_adverse_excursion_pct",
  ];
  const lines = [
    headers.join(","),
    ...trades.map((trade) =>
      headers
        .map((key) => csvCell((trade as Record<string, unknown>)[key]))
        .join(","),
    ),
  ];
  const blob = new Blob([`\uFEFF${lines.join("\n")}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename.replace(/[^\w.-]+/g, "_");
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function csvCell(value: unknown) {
  if (value === null || value === undefined) return "";
  const text = String(value);
  if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function BacktestRunHistory({ runs, onSelect }: { runs: Array<DbRow>; onSelect: (row: DbRow) => void }) {
  return (
    <div className="mt-3 overflow-hidden rounded-2xl border border-[#263246] bg-[#0b1220]">
      <div className="flex h-10 items-center justify-between border-b border-[#e6edf5] px-3 text-xs">
        <span className="font-semibold text-[#e5eefb]">历史回测与寻优记录</span>
        <span className="text-[11px] text-[#7b8798]">来自 SQLite backtest_runs，按当前标的过滤</span>
      </div>
      <div className="max-h-56 overflow-auto">
        {runs.length ? (
          runs.map((row) => {
            const payload = row.payload as Record<string, unknown>;
            const summary = (payload.summary as Record<string, unknown> | undefined) || {};
            const request = (payload.request as Record<string, unknown> | undefined) || {};
            const isOptimization = String(payload.type || "") === "parameter_optimization";
            return (
              <button
                key={row.id}
                className="grid w-full grid-cols-[110px_1fr_78px_78px_70px_72px] gap-2 border-b border-[#1f2a3d] px-3 py-2 text-left text-[11px] hover:bg-[#101a2d]"
                onClick={() => onSelect(row)}
              >
                <span>
                  <span className={`rounded-full px-2 py-1 ${isOptimization ? "bg-[#241806] text-[#facc15]" : "bg-[#102a5c] text-[#2454ff]"}`}>
                    {isOptimization ? "寻优" : "回测"}
                  </span>
                </span>
                <span className="min-w-0 truncate text-[#2f3b52]">
                  {row.created_at} / {String(request.start_date || "--")} 至 {String(request.end_date || "--")} / {String(request.data_source || "--")}
                </span>
                <span className={mono}>收益 {pct(summary.total_return_pct)}</span>
                <span className={mono}>回撤 {pct(summary.max_drawdown_pct)}</span>
                <span className={mono}>交易 {num(summary.trade_count, 0)}</span>
                <span className={mono}>PF {num(summary.profit_factor, 3)}</span>
              </button>
            );
          })
        ) : (
          <div className="px-3 py-6 text-center text-xs text-[#7b8798]">当前标的还没有落库回测记录。</div>
        )}
      </div>
    </div>
  );
}

function OptimizationView({ result }: { result: OptimizationResult }) {
  const best = result.best;
  const params = best?.params || {};
  return (
    <div className="mt-3 rounded-2xl border border-[#263246] bg-[#101a2d] p-3 text-[11px]">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <div className="text-xs font-semibold text-[#e5eefb]">参数寻优结果</div>
          <div className="mt-1 text-[#7b8798]">{String(result.selection_policy || "按验证集收益、回撤、交易数和稳定性综合排序")}</div>
        </div>
        <span className="rounded-full bg-[#0b1220] px-3 py-1 text-[#94a3b8] shadow-sm">{num(result.searched_candidates, 0)} 个候选</span>
      </div>
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-6">
        <Metric label="组合" value={String(params.momentum_filter || "无")} />
        <Metric label="KC长度" value={num(params.kc_length, 0)} />
        <Metric label="KC宽度" value={num(params.kc_scalar, 2)} />
        <Metric label="VOL倍数" value={num(params.volume_multiple, 2)} />
        <Metric label="KDJ周期" value={num(params.kdj_length, 0)} />
        <Metric label="ATR止损" value={num(params.atr_stop_multiple, 2)} />
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 lg:grid-cols-6">
        <Metric label="训练收益" value={pct(best?.train?.total_return_pct)} />
        <Metric label="训练回撤" value={pct(best?.train?.max_drawdown_pct)} />
        <Metric label="训练交易" value={num(best?.train?.trade_count, 0)} />
        <Metric label="验证收益" value={pct(best?.validation?.total_return_pct)} tone={Number(best?.validation?.total_return_pct) >= 0 ? "good" : "bad"} />
        <Metric label="验证回撤" value={pct(best?.validation?.max_drawdown_pct)} tone="warn" />
        <Metric label="验证PF" value={num(best?.validation?.profit_factor, 3)} />
      </div>
      {best?.warnings?.length ? (
        <div className="mt-2 rounded-xl border border-[#854d0e] bg-[#241806] p-2 text-[#facc15]">
          {best.warnings.join(" / ")}
        </div>
      ) : null}
      <div className="mt-3 max-h-36 overflow-auto rounded-xl border border-[#263246] bg-[#0b1220]">
        {(result.candidates || []).map((item, idx) => (
          <div key={idx} className="grid grid-cols-[42px_minmax(0,1fr)_80px_80px_80px_70px] gap-2 border-t border-[#1f2a3d] px-2 py-2 first:border-t-0">
            <span className={mono}>#{idx + 1}</span>
            <span className={`${mono} truncate`}>
              {String(item.params?.momentum_filter || "无")} / KC{num(item.params?.kc_scalar, 2)} / VOL{num(item.params?.volume_multiple, 2)} / ATR{num(item.params?.atr_stop_multiple, 2)}
            </span>
            <span>收益 {pct(item.validation?.total_return_pct)}</span>
            <span>回撤 {pct(item.validation?.max_drawdown_pct)}</span>
            <span>PF {num(item.validation?.profit_factor, 3)}</span>
            <span>分数 {num(item.score, 3)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function arrayValue<T>(value: unknown, fallback: T[]): T[] {
  return Array.isArray(value) && value.length > 0 ? (value as T[]) : fallback;
}

function AiBrainWorkspace({
  symbol,
  status,
  platform,
  profile,
  decisions,
}: {
  symbol: string;
  status: StatusResponse | null;
  platform: PlatformOverview | null;
  profile?: StrategyProfile;
  decisions: Array<DbRow>;
}) {
  const ai = status?.ai || {};
  const latestDecision = status?.latest_decisions?.[symbol]?.payload || decisions[0]?.payload || { state: "等待下一次AI判断" };
  const reviewRuns = platform?.latest_ai_review_runs || [];
  return (
    <section className="min-h-0 space-y-4 overflow-auto sm:space-y-5 sm:pr-1">
      <Surface title={<><BrainCircuit size={13} /> DeepSeek 五档决策中心</>}>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 2xl:grid-cols-7">
          <Metric label="接入状态" value={ai.api_key_configured ? "已配置" : "未配置"} tone={ai.api_key_configured ? "good" : "warn"} />
          <Metric label="常规模型" value={String(ai.decision_model || "--")} />
          <Metric label="突发筛查" value={String(ai.emergency_screening_model || "--")} />
          <Metric label="当前标的" value={shortSymbol(symbol)} />
          <Metric label="策略档案" value={profile?.profile_name || "--"} />
          <Metric label="AI评估记录" value={num(reviewRuns.length, 0)} />
        </div>
        <div className="mt-4">
          <DecisionSummary data={latestDecision} />
        </div>
      </Surface>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <AiLevelCard level="满仓" scale="100%" condition="技术、趋势、新闻、订单流/密集区强共识，AI 置信度达标。" tone="good" />
        <AiLevelCard level="强仓" scale="75%" condition="五分制评分较强，但未满足满仓共识条件。" tone="good" />
        <AiLevelCard level="标准仓" scale="50%" condition="趋势有效，部分确认因子仍需折扣。" tone="warn" />
        <AiLevelCard level="弱仓" scale="25%" condition="刚超过最低交易阈值，只允许小仓验证。" tone="warn" />
        <AiLevelCard level="阻断" scale="0%" condition="震荡/方向冲突/置信度不足/硬风控触发。" tone="bad" />
      </div>

      <div className="grid min-h-0 grid-cols-1 gap-4 2xl:grid-cols-[minmax(0,1fr)_420px]">
        <Surface title={<><ShieldCheck size={13} /> AI 不可越权边界</>}>
          <div className="grid grid-cols-1 gap-3 text-xs text-[#94a3b8] sm:grid-cols-2">
            <Guardrail title="不能绕过策略信号" body="当前真实执行仍以本地趋势策略触发为入口，AI 只能确认、降仓或否决。" />
            <Guardrail title="不能绕过授权" body="冷启动暂停、逐标的授权、同向持仓禁止重复加仓都在本地风控层强制执行。" />
            <Guardrail title="不能突破杠杆上限" body="总名义仓位必须被裁剪到权益乘以全局杠杆上限以内。" />
            <Guardrail title="不能直接晋升参数" body="回测或 AI 建议必须经过验证，不能直接改实盘策略参数。" />
          </div>
          <div className="mt-4">
            <JsonBlock data={latestDecision} maxHeight="max-h-80" />
          </div>
        </Surface>

        <Surface title={<><BrainCircuit size={13} /> 最近 AI 决策</>}>
          <div className="max-h-[520px] overflow-auto">
            {decisions.length ? (
              decisions.slice(0, 24).map((row) => <DecisionRow key={`${row.id}-${row.created_at}`} row={row} />)
            ) : (
              <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">暂无 AI 决策记录。</div>
            )}
          </div>
        </Surface>
      </div>
    </section>
  );
}

function AiLevelCard({
  level,
  scale,
  condition,
  tone,
}: {
  level: string;
  scale: string;
  condition: string;
  tone: "good" | "warn" | "bad";
}) {
  const toneClass = tone === "good" ? "text-[#22c55e]" : tone === "bad" ? "text-[#fb7185]" : "text-[#facc15]";
  return (
    <div className="rounded-2xl border border-[#263246] bg-[#0b1220] p-4 shadow-[0_12px_35px_rgba(26,42,68,0.06)]">
      <div className="text-[11px] uppercase text-[#7b8798]">{level}</div>
      <div className={`${mono} mt-1 text-2xl font-bold ${toneClass}`}>{scale}</div>
      <div className="mt-3 text-xs leading-5 text-[#94a3b8]">{condition}</div>
    </div>
  );
}

function Guardrail({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3">
      <div className="font-semibold text-[#e5eefb]">{title}</div>
      <div className="mt-1 leading-5">{body}</div>
    </div>
  );
}

function DecisionRow({ row }: { row: DbRow }) {
  const payload = row.payload || {};
  const event = payload.event && typeof payload.event === "object" ? (payload.event as Record<string, unknown>) : null;
  const body = payload.ai && typeof payload.ai === "object"
    ? (payload.ai as Record<string, unknown>)
    : payload.payload && typeof payload.payload === "object"
      ? (payload.payload as Record<string, unknown>)
      : payload;
  const risk = payload.risk && typeof payload.risk === "object" ? (payload.risk as Record<string, unknown>) : null;
  const action = String(body.action_suggestion || body.veto_action || body.action || payload.state || "--");
  const regime = String(body.regime || event?.event_type || "--");
  const confidence = body.confidence ?? "--";
  const direction = String(body.direction || "--");
  return (
    <div className="mb-2 rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs">
      <div className="flex items-center justify-between gap-3">
        <div className="font-semibold text-[#e5eefb]">{row.symbol ? shortSymbol(row.symbol) : "系统"}</div>
        <div className={`${mono} text-[11px] text-[#94a3b8]`}>{row.created_at}</div>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-5">
        <Metric label="状态" value={regime} />
        <Metric label="方向" value={direction} />
        <Metric label="动作" value={action} />
        <Metric label="置信度" value={String(confidence)} />
        {risk ? <Metric label="仓位档" value={`${tierLabel(risk.position_tier)} ${positionScaleLabel(risk.position_scale)}`} /> : null}
      </div>
    </div>
  );
}

function AgentGatewayWorkspace({ platform }: { platform: PlatformOverview | null }) {
  const gateway = platform?.platform.agent_gateway;
  const [probe, setProbe] = useState<{ status: number; message: string; ok: boolean } | null>(null);
  const [probing, setProbing] = useState(false);

  const probeHealth = async () => {
    setProbing(true);
    try {
      const response = await fetch("/api/agent/v1/health");
      const body = await response.json().catch(() => ({}));
      setProbe({
        status: response.status,
        ok: response.ok,
        message: String((body as Record<string, unknown>).detail || (body as Record<string, unknown>).message || (response.ok ? "ok" : "blocked")),
      });
    } catch (error) {
      setProbe({ status: 0, ok: false, message: errText(error) });
    } finally {
      setProbing(false);
    }
  };

  return (
    <section className="min-h-0 space-y-5 overflow-auto pr-1">
      <Surface
        title={<><KeyRound size={13} /> 智能体网关控制台</>}
        action={<button className={button} disabled={probing} onClick={probeHealth}>{probing ? "探测中" : "探测健康接口"}</button>}
      >
        <div className="grid grid-cols-6 gap-3">
          <Metric label="版本" value={gateway?.version || "agent/v1"} />
          <Metric label="访问令牌" value={gateway?.enabled ? "已配置" : "未配置"} tone={gateway?.enabled ? "warn" : "good"} />
          <Metric label="权限范围" value={(gateway?.scopes || ["R", "B"]).join("/")} tone="good" />
          <Metric label="纸面模式" value={gateway?.paper_only ? "强制" : "未知"} tone="good" />
          <Metric label="实盘交易" value={gateway?.live_trading || "拒绝"} tone="good" />
          <Metric label="探测结果" value={probe ? `HTTP ${probe.status}` : "未探测"} tone={probe?.ok ? "good" : probe ? "warn" : "default"} />
        </div>
        {probe ? (
          <div className="mt-3 rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">
            健康接口返回：{probe.message}。浏览器端不保存智能体访问令牌；如果这里返回 503/401，说明网关未配置或未授权，属于安全阻断。
          </div>
        ) : null}
      </Surface>

      <div className="grid grid-cols-4 gap-3">
        <AgentCapability title="读取行情" scope="R" status="允许" body="读取市场、策略档案、任务状态。" tone="good" />
        <AgentCapability title="纸面回测" scope="B" status="允许" body="必须带幂等键，不触碰交易所。" tone="good" />
        <AgentCapability title="读取密钥" scope="-" status="拒绝" body="密钥只在服务端环境，不能经智能体暴露。" tone="bad" />
        <AgentCapability title="真实下单" scope="-" status="拒绝" body="智能体默认永远不能直接实盘交易。" tone="bad" />
      </div>

      <div className="grid min-h-0 grid-cols-[minmax(0,1fr)_420px] gap-5 overflow-hidden">
        <Surface title={<><ServerCog size={13} /> 智能体 API 清单</>}>
          <div className="grid gap-2">
            <EndpointRow method="GET" path="/api/agent/v1/health" scope="R" note="健康检查；需要 Bearer Token。" />
            <EndpointRow method="GET" path="/api/agent/v1/strategy-profiles" scope="R" note="读取策略档案；只读。" />
            <EndpointRow method="POST" path="/api/agent/v1/backtests" scope="B" note="启动纸面回测；必须带 Idempotency-Key。" />
            <EndpointRow method="GET" path="/api/agent/v1/backtests/{job_id}" scope="R" note="读取智能体创建的回测任务。" />
          </div>
          <div className="mt-4 grid grid-cols-2 gap-3">
            <Guardrail title="令牌不进前端" body="浏览器界面只显示是否配置，不显示、不输入、不缓存智能体访问令牌。" />
            <Guardrail title="幂等键必需" body="智能体启动回测必须带 Idempotency-Key，避免网络重试造成重复任务。" />
            <Guardrail title="审计必需" body="每个智能体调用写入 agent_audit_events，记录路由、权限、任务、幂等尾号。" />
            <Guardrail title="实盘隔离" body="智能体路由没有下单能力；真实交易仍只走控制台核心执行链路。" />
          </div>
        </Surface>

        <Surface title={<><FlaskConical size={13} /> 最近纸面任务</>}>
          <div className="max-h-[520px] overflow-auto">
            {(platform?.latest_backtest_runs || []).length ? (
              (platform?.latest_backtest_runs || []).map((row) => <AgentBacktestRun key={`${row.id}-${row.created_at}`} row={row} />)
            ) : (
              <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">暂无回测任务。</div>
            )}
          </div>
        </Surface>
      </div>
    </section>
  );
}

function AgentCapability({
  title,
  scope,
  status,
  body,
  tone,
}: {
  title: string;
  scope: string;
  status: string;
  body: string;
  tone: "good" | "bad";
}) {
  const toneClass = tone === "good" ? "text-[#22c55e]" : "text-[#fb7185]";
  return (
    <div className="rounded-2xl border border-[#263246] bg-[#0b1220] p-4 shadow-[0_12px_35px_rgba(26,42,68,0.06)]">
      <div className="flex items-center justify-between gap-3">
        <div className="font-semibold text-[#e5eefb]">{title}</div>
        <span className={`${mono} rounded-full border border-[#263246] bg-[#101a2d] px-2 py-1 text-[11px]`}>{scope}</span>
      </div>
      <div className={`mt-2 text-xl font-bold ${toneClass}`}>{status}</div>
      <div className="mt-2 text-xs leading-5 text-[#94a3b8]">{body}</div>
    </div>
  );
}

function EndpointRow({ method, path, scope, note }: { method: string; path: string; scope: string; note: string }) {
  return (
    <div className="grid grid-cols-[70px_minmax(0,1fr)_64px] items-center gap-3 rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs">
      <span className={`${mono} font-semibold text-[#2454ff]`}>{method}</span>
      <div>
        <div className={`${mono} truncate font-semibold text-[#e5eefb]`}>{path}</div>
        <div className="mt-1 text-[#94a3b8]">{note}</div>
      </div>
      <span className="rounded-full bg-[#0b1220] px-2 py-1 text-center text-[11px] text-[#94a3b8]">scope {scope}</span>
    </div>
  );
}

function AgentBacktestRun({ row }: { row: DbRow }) {
  const payload = row.payload || {};
  const summary = payload.summary && typeof payload.summary === "object" ? (payload.summary as Record<string, unknown>) : {};
  return (
    <div className="mb-2 rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs">
      <div className="flex items-center justify-between gap-3">
        <div className="font-semibold text-[#e5eefb]">{shortSymbol(String(row.symbol || payload.symbol || "--"))}</div>
        <div className={`${mono} text-[11px] text-[#94a3b8]`}>{row.created_at}</div>
      </div>
      <div className="mt-2 grid grid-cols-3 gap-2">
        <Metric label="收益率" value={pct(summary.total_return_pct)} />
        <Metric label="回撤" value={pct(summary.max_drawdown_pct)} />
        <Metric label="交易数" value={num(summary.trade_count, 0)} />
      </div>
    </div>
  );
}

function ExecutionWorkspace({
  symbol,
  platform,
  status,
  balance,
  followerBalance,
  positions,
  orders,
  accountSlots,
  riskSummary,
  visibleSlots,
  isAdmin,
  busy,
  postAction,
}: {
  symbol: string;
  platform: PlatformOverview | null;
  status: StatusResponse | null;
  balance: Record<string, unknown> | null;
  followerBalance: Record<string, unknown> | null;
  positions: Array<DbRow>;
  orders: Array<DbRow>;
  accountSlots: ExecutionAccountSlot[];
  riskSummary: Record<string, unknown> | null;
  visibleSlots: Array<"trend" | "follower" | "range">;
  isAdmin: boolean;
  busy: boolean;
  postAction: (path: string, body: Record<string, unknown>) => Promise<void>;
}) {
  const usdt = balance?.USDT && typeof balance.USDT === "object" ? (balance.USDT as Record<string, unknown>) : {};
  const followerUsdt = followerBalance?.USDT && typeof followerBalance.USDT === "object" ? (followerBalance.USDT as Record<string, unknown>) : {};
  const executionMode = status?.execution_mode || platform?.platform.execution_mode || "mock";
  const enabled = status?.enabled_symbols || [];
  const riskCap = Number(riskSummary?.max_total_leverage ?? status?.risk?.max_total_leverage ?? 4);
  const totalEquity = Number(balance?.total_usdt ?? balance?.usdt_total ?? usdt.total ?? 0);
  const followerEquity = Number(followerBalance?.total_usdt ?? followerBalance?.usdt_total ?? followerUsdt.total ?? 0);
  const maxNotional = totalEquity > 0 ? totalEquity * riskCap : 0;
  const channels = platform?.strategy_channels || [];
  const visibleChannels = channels.filter((item) => visibleSlots.includes(item.account_slot));
  const trendChannel = channels.find((item) => item.channel === "trend");
  const followerChannel = channels.find((item) => item.channel === "follower");
  const rangeChannel = channels.find((item) => item.channel === "range");
  return (
    <section className="min-h-0 space-y-4 overflow-auto sm:space-y-5 sm:pr-1">
      <Surface title={<><ShieldCheck size={13} /> 实盘安全链路</>}>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 2xl:grid-cols-6">
          <Metric label="执行模式" value={executionMode === "live" ? "真实网关" : "模拟网关"} tone={executionMode === "live" ? "warn" : "good"} />
          <Metric label="交易模式" value={status?.trade_mode || platform?.platform.trade_mode || "--"} />
          <Metric label="开仓状态" value={status?.opening_paused ? "已暂停" : "允许"} tone={status?.opening_paused ? "warn" : "good"} />
          <Metric label="授权标的" value={`${enabled.length}`} />
          <Metric label="账号1权益" value={num(totalEquity)} />
          <Metric label="账号2权益" value={followerBalance?.ok === false ? "未配置" : num(followerEquity)} tone={followerBalance?.ok === false ? "warn" : undefined} />
          <Metric label="最大名义" value={num(maxNotional)} />
        </div>
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <ExecutionStep title="控制台/智能体" body="只能提交动作请求，不能直接碰交易所密钥。" done />
          <ExecutionStep title="策略信号" body="必须来自本地策略引擎，AI 不能凭空开仓。" done />
          <ExecutionStep title="AI 五档" body="只允许确认、降仓、阻断，不允许突破硬风控。" done />
          <ExecutionStep title="本地风控" body="授权、同向持仓、杠杆上限、置信度统一裁剪。" done />
          <ExecutionStep title="交易网关" body={executionMode === "live" ? "Gate.io 真实网关" : "本地 Mock 网关"} done={executionMode === "mock"} warn={executionMode === "live"} />
        </div>
      </Surface>

      <div className="grid grid-cols-1 gap-5 2xl:grid-cols-[minmax(0,1fr)_430px]">
        <Surface title={<><ServerCog size={13} /> 策略运行通道</>}>
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {visibleSlots.includes("trend") ? <StrategyChannelCard
              title="趋势策略运行"
              account={trendChannel?.account_label || "账号1：趋势策略 API"}
              status={trendChannel?.live_ready ? "实盘就绪" : trendChannel?.executable ? "可运行" : "阻断"}
              body="当前 ETH 趋势引擎入口。策略信号触发后，DeepSeek 五档仓位确认，再进入本地风控和趋势账号交易网关。"
              enabled={Boolean(trendChannel?.executable)}
              liveReady={Boolean(trendChannel?.live_ready)}
              details={[
                `授权标的 ${trendChannel?.authorized_symbols?.length || enabled.length}`,
                `账户 ${trendChannel?.account_configured ? "已配置" : "未配置"}`,
                `单账户上限 ${num(riskCap, 1)}x`,
              ]}
            /> : null}
            {visibleSlots.includes("follower") ? <StrategyChannelCard
              title="账号2跟随执行"
              account={followerChannel?.account_label || "账号2：趋势跟随 API"}
              status={followerChannel?.live_ready ? "跟随就绪" : followerChannel?.executable ? "等待主账户信号" : "未启用"}
              body="账号2不单独计算策略，也不重复调用 DeepSeek。账号1通过策略、AI 和风控后，账号2复用同一订单意图并按自己的余额、杠杆上限和跟随比例裁剪仓位。"
              enabled={Boolean(followerChannel?.executable)}
              liveReady={Boolean(followerChannel?.live_ready)}
              details={[
                `账户 ${followerChannel?.account_configured ? "已配置" : "未配置"}`,
                "AI 决策 复用账号1",
                `路由 ${followerChannel?.gateway_binding === "active" ? "已备好" : "待配置"}`,
              ]}
            /> : null}
            {visibleSlots.includes("range") ? <StrategyChannelCard
              title="震荡策略预留"
              account={rangeChannel?.account_label || "账号3：震荡策略 API"}
              status={rangeChannel?.status || "预留"}
              body="震荡策略账户通道已保留，但当前没有生产级震荡策略信号。未来接入震荡策略后，仍会复用同一套账号登录、AI 裁剪和 Gateway 风控。"
              enabled={Boolean(rangeChannel?.enabled)}
              liveReady={Boolean(rangeChannel?.live_ready)}
              details={[
                `账户 ${rangeChannel?.account_configured ? "已配置" : "未配置"}`,
                "当前禁止自动执行",
                `单账户上限 ${num(accountSlots.find((item) => item.slot === "range")?.max_leverage ?? 4, 1)}x`,
              ]}
            /> : null}
          </div>
          <div className="mt-3 rounded-xl border border-[#854d0e] bg-[#241806] p-3 text-xs leading-5 text-[#facc15]">
            当前按你的要求采用“一次策略信号 + 一次 DeepSeek 决策 + 多账户独立裁剪”：账号1先执行，账号2跟随复制同一订单意图。每个 Gate 账户按自己的余额、杠杆上限和跟随比例单独计算仓位。
          </div>
        </Surface>
        <RuntimeModePanel executionMode={executionMode} isAdmin={isAdmin} busy={busy} postAction={postAction} />
      </div>

      <AccountSlotManager accountSlots={accountSlots} visibleSlots={visibleSlots} isAdmin={isAdmin} busy={busy} postAction={postAction} />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 2xl:grid-cols-4">
        <Surface title={<><Wallet size={13} /> 账户与风控</>}>
          <div className="grid grid-cols-2 gap-3">
            <Metric label="账号1 USDT总额" value={num(balance?.total_usdt ?? balance?.usdt_total ?? usdt.total)} />
            <Metric label="账号1 USDT可用" value={num(balance?.free_usdt ?? balance?.usdt_free ?? usdt.free)} />
            <Metric label="账号2 USDT总额" value={followerBalance?.ok === false ? "未配置" : num(followerBalance?.total_usdt ?? followerBalance?.usdt_total ?? followerUsdt.total)} tone={followerBalance?.ok === false ? "warn" : undefined} />
            <Metric label="账号2 USDT可用" value={followerBalance?.ok === false ? "--" : num(followerBalance?.free_usdt ?? followerBalance?.usdt_free ?? followerUsdt.free)} />
            <Metric label="杠杆硬上限" value={`${num(riskCap, 1)}x`} />
            <Metric label="最低AI置信度" value={num(riskSummary?.min_confidence_to_trade ?? status?.risk?.min_confidence_to_trade, 2)} />
          </div>
        </Surface>
        {isAdmin ? <Surface title={<><Power size={13} /> 人工危险操作</>}>
          <div className="grid gap-2">
            <button className={danger} disabled={busy} onClick={() => postAction("/api/control/close-position", { operator_id: "console", symbol })}>
              平仓 {shortSymbol(symbol)}
            </button>
            <button className={danger} disabled={busy} onClick={() => postAction("/api/control/panic-close", { operator_id: "console", symbols: [] })}>
              暂停开仓并一键全平
            </button>
            <div className="text-[11px] leading-5 text-[#94a3b8]">这里不提供手动开仓入口。真实开仓必须由策略信号、AI、本地风控和交易网关串联通过。</div>
          </div>
        </Surface> : <Surface title={<><Power size={13} /> 交易权限</>}>
          <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs leading-5 text-[#94a3b8]">
            当前账号只能查看交易链路和修改自己账户的杠杆上限，不能切换实盘/模拟、不能手动平仓、不能修改策略参数。
          </div>
        </Surface>}
        <Surface title={<><ServerCog size={13} /> 网关边界</>}>
          <div className="grid gap-2 text-xs text-[#94a3b8]">
            <BoundaryLine label="可见通道" value={`${visibleChannels.length}/${channels.length || visibleSlots.length}`} />
            <BoundaryLine label="业务层" value="不写 dry_run 分支" />
            <BoundaryLine label="模拟运行" value="本地模拟网关，本地记账" />
            <BoundaryLine label="真实运行" value="Gate.io 真实网关，固定 dry_run=false" />
            <BoundaryLine label="智能体" value="默认只读/纸面回测" />
          </div>
        </Surface>
        <Surface title={<><Database size={13} /> 审计要求</>}>
          <div className="grid gap-2 text-xs text-[#94a3b8]">
            <BoundaryLine label="订单" value="orders 表留痕" />
            <BoundaryLine label="AI" value="ai_decisions 表留痕" />
            <BoundaryLine label="风控" value="本地风控结论可追踪" />
            <BoundaryLine label="密钥" value="不得进入日志/页面" />
          </div>
        </Surface>
      </div>

      <div className="grid grid-cols-1 gap-5 2xl:grid-cols-[minmax(0,1fr)_460px]">
        <Surface title={<><Wallet size={13} /> 持仓快照</>}>
          <div className="max-h-[440px] overflow-auto">
            {positions.length ? (
              <table className="min-w-[720px] w-full text-left text-xs">
                <thead className="sticky top-0 bg-[#101a2d] text-[#94a3b8]">
                  <tr><th className="p-2">标的</th><th>方向</th><th>数量</th><th>开仓价</th><th>标记价</th><th>未实现盈亏</th></tr>
                </thead>
                <tbody>
                  {positions.map((row) => <PositionRow key={`${row.id}-${row.symbol}`} row={row} />)}
                </tbody>
              </table>
            ) : (
              <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">暂无持仓快照。模拟网关无持仓时这是正常状态。</div>
            )}
          </div>
        </Surface>
        <Surface title={<><Power size={13} /> 最近订单</>}>
          <div className="max-h-[440px] overflow-auto">
            {orders.length ? orders.slice(0, 30).map((row) => <OrderRow key={`${row.id}-${row.created_at}`} row={row} />) : (
              <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">暂无订单记录。</div>
            )}
          </div>
        </Surface>
      </div>
    </section>
  );
}

function StrategyChannelCard({
  title,
  account,
  status,
  body,
  enabled = false,
  liveReady = false,
  details = [],
}: {
  title: string;
  account: string;
  status: string;
  body: string;
  enabled?: boolean;
  liveReady?: boolean;
  details?: string[];
}) {
  return (
    <div className="rounded-2xl border border-[#263246] bg-[#101a2d] p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="font-semibold text-[#e5eefb]">{title}</div>
        <span className={`rounded-full px-3 py-1 text-[11px] ${liveReady ? "bg-[#052e1a] text-[#22c55e]" : enabled ? "bg-[#102a5c] text-[#2454ff]" : "bg-[#241806] text-[#facc15]"}`}>
          {status}
        </span>
      </div>
      <div className={`${mono} mt-2 text-xs text-[#2454ff]`}>{account}</div>
      <div className="mt-2 text-xs leading-5 text-[#94a3b8]">{body}</div>
      {details.length ? (
        <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
          {details.map((detail) => (
            <div key={detail} className="rounded-xl border border-[#263246] bg-[#0b1220] px-3 py-2 text-[11px] text-[#94a3b8]">
              {detail}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function RuntimeModePanel({
  executionMode,
  isAdmin,
  busy,
  postAction,
}: {
  executionMode: string;
  isAdmin: boolean;
  busy: boolean;
  postAction: (path: string, body: Record<string, unknown>) => Promise<void>;
}) {
  const isLive = executionMode === "live";
  const isMock = executionMode === "mock";
  return (
    <Surface title={<><Power size={13} /> 模拟 / 实盘模式</>}>
      <div className="grid grid-cols-2 gap-2">
        <button
          className={`${button} justify-center ${isMock ? "border-[#22c55e] bg-[#052e1a] text-[#86efac]" : ""}`}
          disabled={busy || isMock}
          onClick={() => postAction("/api/control/runtime-mode", { operator_id: "console", dry_run: true })}
        >
          {isMock ? "当前模拟" : "切回模拟"}
        </button>
        <button
          className={`${button} justify-center ${isLive ? "border-[#facc15] bg-[#241806] text-[#facc15]" : ""}`}
          disabled={busy || !isAdmin || isLive}
          onClick={() => postAction("/api/control/runtime-mode", { operator_id: "console", dry_run: false })}
        >
          {isLive ? "当前实盘" : "开启实盘"}
        </button>
      </div>
      <div className="mt-2 text-[11px] leading-5 text-[#94a3b8]">
        实盘/模拟切换不再使用 Trade PIN 或操作验证码。只有管理员账号可以切换；普通账号只能查看和调整自己账户的杠杆上限。
      </div>
      {isLive ? (
        <div className="mt-2 rounded-lg border border-[#854d0e] bg-[#241806] px-2 py-1.5 text-[11px] leading-5 text-[#facc15]">
          当前已经切到实盘网关；能否自动开仓仍取决于授权标的、readiness、AI 五档和本地风控，不等于立即允许下单。
        </div>
      ) : null}
      <div className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
        <div className={`rounded-lg border px-2 py-1.5 ${isAdmin ? "border-[#14532d] bg-[#052e1a] text-[#86efac]" : "border-[#854d0e] bg-[#241806] text-[#facc15]"}`}>
          当前权限：{isAdmin ? "管理员" : "只读账户"}
        </div>
        <div className={`rounded-lg border px-2 py-1.5 ${isLive ? "border-[#854d0e] bg-[#241806] text-[#facc15]" : "border-[#14532d] bg-[#052e1a] text-[#86efac]"}`}>
          当前模式：{isLive ? "实盘" : "模拟"}
        </div>
      </div>
    </Surface>
  );
}

function AccountSlotManager({
  accountSlots,
  visibleSlots,
  isAdmin,
  busy,
  postAction,
}: {
  accountSlots: ExecutionAccountSlot[];
  visibleSlots: Array<"trend" | "follower" | "range">;
  isAdmin: boolean;
  busy: boolean;
  postAction: (path: string, body: Record<string, unknown>) => Promise<void>;
}) {
  return (
    <Surface title={<><KeyRound size={13} /> 账户 API 与杠杆槽位</>}>
      <div className="grid grid-cols-1 gap-3 2xl:grid-cols-3">
        {visibleSlots.map((slot) => (
          <AccountSlotCard
            key={slot}
            slot={slot}
            item={accountSlots.find((account) => account.slot === slot)}
            canEditSecret={isAdmin}
            canEditLeverage
            busy={busy}
            postAction={postAction}
          />
        ))}
      </div>
    </Surface>
  );
}

function AccountSlotCard({
  slot,
  item,
  canEditSecret,
  canEditLeverage,
  busy,
  postAction,
}: {
  slot: "trend" | "follower" | "range";
  item?: ExecutionAccountSlot;
  canEditSecret: boolean;
  canEditLeverage: boolean;
  busy: boolean;
  postAction: (path: string, body: Record<string, unknown>) => Promise<void>;
}) {
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [maxLeverage, setMaxLeverage] = useState(String(item?.max_leverage ?? 4));
  const label = accountSlotUiLabel(slot);
  useEffect(() => {
    setMaxLeverage(String(item?.max_leverage ?? 4));
  }, [item?.max_leverage, slot]);
  const submit = async () => {
    await postAction("/api/execution/accounts/secret", {
      operator_id: "console",
      account_slot: slot,
      exchange: "gateio",
      api_key: apiKey,
      api_secret: apiSecret,
    });
    setApiKey("");
    setApiSecret("");
  };
  const updateLeverage = async () => {
    const value = Number(maxLeverage);
    if (!Number.isFinite(value) || value <= 0) return;
    await postAction("/api/execution/accounts/leverage", {
      operator_id: "console",
      account_slot: slot,
      max_leverage: value,
    });
  };
  return (
    <div className="rounded-2xl border border-[#263246] bg-[#101a2d] p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="font-semibold text-[#e5eefb]">{item?.label || label}</div>
          <div className="mt-1 text-[11px] text-[#7b8798]">Gate.io USDT 永续 / {accountSlotStrategyLabel(slot)}</div>
        </div>
        <span className={`rounded-full px-3 py-1 text-[11px] ${item?.configured ? "bg-[#052e1a] text-[#22c55e]" : "bg-[#241806] text-[#facc15]"}`}>
          {item?.configured ? "已配置" : "未配置"}
        </span>
      </div>
      <div className="mt-3 grid grid-cols-1 gap-2 text-xs sm:grid-cols-3">
        <Metric label="版本" value={num(item?.version, 0)} />
        <Metric label="Key尾号" value={item?.key_tail || "-"} />
        <Metric label="实盘路由" value={item?.gateway_binding === "active" ? "已绑定" : "待绑定"} tone={item?.gateway_binding === "active" ? "good" : "warn"} />
      </div>
      <div className="mt-3 grid grid-cols-[minmax(0,1fr)_96px] gap-2">
        <input
          className={`${input} ${mono}`}
          type="number"
          min={0.1}
          max={50}
          step={0.1}
          value={maxLeverage}
          onChange={(event) => setMaxLeverage(event.target.value)}
          disabled={!canEditLeverage}
          placeholder="杠杆上限"
        />
        <button className={button} disabled={busy || !canEditLeverage || Number(maxLeverage) <= 0} onClick={updateLeverage}>
          更新杠杆
        </button>
      </div>
      {item?.credential_source === "legacy_default_gateio" ? (
        <div className="mt-2 rounded-xl border border-[#854d0e] bg-[#241806] px-3 py-2 text-[11px] leading-5 text-[#facc15]">
          正在兼容使用旧版 GATEIO_API_KEY 作为趋势账号。建议后续迁移到 GATEIO_TREND_API_KEY，便于双账户隔离。
        </div>
      ) : null}
      {canEditSecret ? <div className="mt-3 grid grid-cols-1 gap-2 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_96px]">
        <input className={`${input} ${mono}`} type="password" placeholder="API Key" value={apiKey} onChange={(event) => setApiKey(event.target.value)} />
        <input className={`${input} ${mono}`} type="password" placeholder="API Secret" value={apiSecret} onChange={(event) => setApiSecret(event.target.value)} />
        <button className={button} disabled={busy || apiKey.length < 8 || apiSecret.length < 8} onClick={submit}>
          更新
        </button>
      </div> : null}
      <div className="mt-2 text-[11px] text-[#94a3b8]">
        {canEditSecret ? "明文只写入运行密钥文件；SQLite 和审计日志只保存指纹与尾号。" : "当前账号不能查看或修改 API 密钥，只能修改本账户杠杆上限。"}
      </div>
    </div>
  );
}

function accountSlotUiLabel(slot: "trend" | "follower" | "range") {
  if (slot === "trend") return "账号1：趋势策略";
  if (slot === "follower") return "账号2：趋势跟随";
  return "账号3：震荡策略预留";
}

function accountSlotStrategyLabel(slot: "trend" | "follower" | "range") {
  if (slot === "trend") return "趋势策略";
  if (slot === "follower") return "趋势跟随";
  return "震荡策略预留";
}

function ExecutionStep({ title, body, done = false, warn = false }: { title: string; body: string; done?: boolean; warn?: boolean }) {
  const tone = warn ? "text-[#facc15]" : done ? "text-[#22c55e]" : "text-[#94a3b8]";
  return (
    <div className="rounded-2xl border border-[#263246] bg-[#101a2d] p-3">
      <div className={`text-sm font-semibold ${tone}`}>{title}</div>
      <div className="mt-2 text-xs leading-5 text-[#94a3b8]">{body}</div>
    </div>
  );
}

function BoundaryLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3 rounded-xl border border-[#263246] bg-[#101a2d] px-3 py-2 text-[#94a3b8]">
      <span>{label}</span>
      <span className="font-medium text-[#e5eefb]">{value}</span>
    </div>
  );
}

function PositionRow({ row }: { row: DbRow }) {
  const payload = row.payload || {};
  return (
    <tr className="border-t border-[#263246]">
      <td className="p-2 font-medium text-[#e5eefb]">{shortSymbol(String(row.symbol || payload.symbol || "--"))}</td>
      <td>{String(payload.side || "--")}</td>
      <td className={mono}>{num(payload.qty, 6)}</td>
      <td className={mono}>{num(payload.entry_price)}</td>
      <td className={mono}>{num(payload.mark_price)}</td>
      <td className={`${mono} ${Number(payload.unrealized_pnl || 0) >= 0 ? "text-[#22c55e]" : "text-[#fb7185]"}`}>{num(payload.unrealized_pnl)}</td>
    </tr>
  );
}

function OrderRow({ row }: { row: DbRow }) {
  const payload = row.payload || {};
  const side = String(payload.side || payload.action || "--");
  const status = String(payload.status || "--");
  return (
    <div className="mb-2 rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs">
      <div className="flex items-center justify-between gap-3">
        <div className="font-semibold text-[#e5eefb]">{shortSymbol(String(row.symbol || payload.symbol || "--"))}</div>
        <div className={`${mono} text-[11px] text-[#94a3b8]`}>{row.created_at}</div>
      </div>
      <div className="mt-2 grid grid-cols-4 gap-2">
        <Metric label="方向" value={side} />
        <Metric label="数量" value={num(payload.amount || payload.qty, 6)} />
        <Metric label="价格" value={num(payload.price)} />
        <Metric label="状态" value={status} />
      </div>
    </div>
  );
}

function DataWorkspace({
  platform,
  status,
  markets,
  news,
  candles,
  warning,
  source,
  timeframe,
  balance,
  riskSummary,
  positions,
  orders,
  busy,
  postAction,
}: {
  platform: PlatformOverview | null;
  status: StatusResponse | null;
  markets: MarketSymbolsResponse;
  news: NewsResponse;
  candles: Candle[];
  warning: string;
  source: string;
  timeframe: string;
  balance: Record<string, unknown> | null;
  riskSummary: Record<string, unknown> | null;
  positions: Array<DbRow>;
  orders: Array<DbRow>;
  busy: boolean;
  postAction: (path: string, body: Record<string, unknown>) => Promise<void>;
}) {
  const latestCandle = candles.at(-1);
  const visibleItems = visibleNewsItems(news);
  const latestNews = visibleItems.at(0);
  const newsWarnings = (news.warnings || []).filter((item) => !isInternalNewsText(item));
  const newsWarn = newsWarnings.length > 0;
  const aiConfigured = Boolean(status?.ai?.api_key_configured);
  const gatewayOk = Boolean(balance?.ok ?? true);
  const marketOk = candles.length > 0 && !warning;
  const newsOk = !newsWarn && visibleItems.length > 0;
  const riskOk = Boolean(riskSummary) && Number(riskSummary?.max_total_leverage || status?.risk?.max_total_leverage || 0) > 0;
  return (
    <section className="min-h-0 space-y-3 overflow-auto sm:space-y-4">
      <Surface
        title={<><Newspaper size={13} /> 新闻快讯</>}
        action={<button className={button} disabled={busy} onClick={() => postAction("/api/news/refresh", { operator_id: "console" })}>刷新</button>}
      >
        {newsWarnings.length ? (
          <div className="mb-3 rounded-xl border border-[#854d0e] bg-[#241806] p-3 text-xs text-[#facc15]">{newsWarnings.join("; ")}</div>
        ) : null}
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
          {visibleItems.length ? visibleItems.slice(0, 12).map((item, idx) => <DashboardNewsItem key={idx} item={item} />) : (
            <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">暂无新闻快讯。</div>
          )}
        </div>
      </Surface>

      <Surface title={<><Database size={13} /> 数据健康总览</>}>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <Metric label="K线源" value={marketOk ? "健康" : "告警"} tone={marketOk ? "good" : "warn"} />
          <Metric label="新闻源" value={newsOk ? "健康" : "需检查"} tone={newsOk ? "good" : "warn"} />
          <Metric label="DeepSeek" value={aiConfigured ? "已配置" : "未配置"} tone={aiConfigured ? "good" : "warn"} />
          <Metric label="交易网关" value={gatewayOk ? "可用" : "异常"} tone={gatewayOk ? "good" : "bad"} />
          <Metric label="风控配置" value={riskOk ? "有效" : "缺失"} tone={riskOk ? "good" : "bad"} />
          <Metric label="审计记录" value={`${orders.length} 单`} />
        </div>
      </Surface>

      <div className="grid min-h-0 grid-cols-1 gap-4">
        <Surface title={<><ServerCog size={13} /> 外部依赖状态</>}>
          <div className="grid grid-cols-1 gap-3 2xl:grid-cols-2">
            <HealthCard
              title="行情 K线"
              status={marketOk ? "健康" : "降级"}
              tone={marketOk ? "good" : "warn"}
              rows={[
                ["数据源", source],
                ["周期", timeframe],
                ["K线数量", num(candles.length, 0)],
                ["最新时间", String(latestCandle?.time || "--")],
                ["告警", warning || "无"],
              ]}
            />
            <HealthCard
              title="新闻快讯"
              status={newsOk ? "健康" : "需检查"}
              tone={newsOk ? "good" : "warn"}
              rows={[
                ["快讯数量", num(visibleItems.length, 0)],
                ["缓存年龄", news.age_minutes == null ? "--" : `${num(news.age_minutes, 1)} 分钟`],
                ["最新消息", String(latestNews?.title || latestNews?.headline || latestNews?.summary || "--").slice(0, 48)],
                ["告警", newsWarnings.join("; ") || "无"],
              ]}
            />
            <HealthCard
              title="DeepSeek"
              status={aiConfigured ? "已接入" : "未配置"}
              tone={aiConfigured ? "good" : "warn"}
              rows={[
                ["常规模型", String(status?.ai?.decision_model || "--")],
                ["报告模型", String(status?.ai?.report_model || "--")],
                ["突发筛查", String(status?.ai?.emergency_screening_model || "--")],
                ["状态", String(status?.ai?.status_message || "--")],
              ]}
            />
            <HealthCard
              title="交易网关"
              status={gatewayOk ? "可用" : "异常"}
              tone={gatewayOk ? "good" : "bad"}
              rows={[
                ["执行模式", status?.execution_mode || platform?.platform.execution_mode || "--"],
                ["账户返回", String(balance?.ok ?? "--")],
                ["USDT总额", num(balance?.total_usdt ?? balance?.usdt_total)],
                ["通知通道", (platform?.platform.notification_channels || []).length ? "外部" : "仅本地"],
              ]}
            />
          </div>
        </Surface>

        <Surface title={<><ShieldCheck size={13} /> 本地核心状态</>}>
          <div className="grid grid-cols-1 gap-3 2xl:grid-cols-3">
            <HealthCard
              title="本地风控"
              status={riskOk ? "有效" : "异常"}
              tone={riskOk ? "good" : "bad"}
              rows={[
                ["开仓状态", status?.opening_paused ? "已暂停" : "允许"],
                ["授权标的", String(status?.enabled_symbols?.length || 0)],
                ["杠杆上限", `${num(riskSummary?.max_total_leverage ?? status?.risk?.max_total_leverage, 1)}x`],
                ["最低AI置信度", num(riskSummary?.min_confidence_to_trade ?? status?.risk?.min_confidence_to_trade, 2)],
              ]}
            />
            <HealthCard
              title="任务与审计"
              status="运行中"
              tone="good"
              rows={[
                ["回测任务", num(platform?.latest_backtest_runs.length, 0)],
                ["AI评估任务", num(platform?.latest_ai_review_runs.length, 0)],
                ["持仓快照", num(positions.length, 0)],
                ["订单记录", num(orders.length, 0)],
              ]}
            />
            <HealthCard
              title="标的配置"
              status={markets.items.length ? "已加载" : "空"}
              tone={markets.items.length ? "good" : "warn"}
              rows={[
                ["标的数量", num(markets.items.length, 0)],
                ["已启用策略", num(markets.items.filter((item) => item.strategy_enabled).length, 0)],
                ["配置标的", markets.items.map((item) => item.base).join(" / ") || "--"],
              ]}
            />
          </div>
        </Surface>
      </div>
    </section>
  );
}

function HealthCard({
  title,
  status,
  tone,
  rows,
}: {
  title: string;
  status: string;
  tone: "good" | "warn" | "bad";
  rows: Array<[string, string]>;
}) {
  const toneClass = tone === "good" ? "bg-[#052e1a] text-[#22c55e]" : tone === "bad" ? "bg-[#2a0f14] text-[#fb7185]" : "bg-[#241806] text-[#facc15]";
  return (
    <div className="rounded-2xl border border-[#263246] bg-[#101a2d] p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="font-semibold text-[#e5eefb]">{title}</div>
        <span className={`rounded-full px-3 py-1 text-[11px] font-medium ${toneClass}`}>{status}</span>
      </div>
      <div className="mt-3 grid gap-2 text-xs">
        {rows.map(([label, value]) => (
          <BoundaryLine key={label} label={label} value={value} />
        ))}
      </div>
    </div>
  );
}

function RightRail({
  symbol,
  status,
  decisions,
  positions,
  orders,
  news,
  denseZone,
  busy,
  postAction,
}: {
  symbol: string;
  status: StatusResponse | null;
  decisions: Array<DbRow>;
  positions: Array<DbRow>;
  orders: Array<DbRow>;
  news: NewsResponse;
  denseZone: DbRow<DenseZonePayload> | null;
  busy: boolean;
  postAction: (path: string, body: Record<string, unknown>) => Promise<void>;
}) {
  const latestDecision = status?.latest_decisions?.[symbol]?.payload || decisions[0]?.payload || { state: "等待下一次AI判断" };
  const newsWarnings = (news.warnings || []).filter((item) => !isInternalNewsText(item));
  const railNewsItems = visibleNewsItems(news).slice(0, 3);
  const position = positionSnapshot(positions, symbol);
  return (
    <aside className="flex min-h-0 flex-col gap-3 overflow-auto">
      <Surface title={<><BrainCircuit size={13} /> AI 决策</>}>
        <DecisionRailSummary data={latestDecision} />
      </Surface>
      <Surface title={<><ShieldCheck size={13} /> 仓位档位</>}>
        <AiSizingRail data={latestDecision} />
      </Surface>
      <Surface
        title={<><Newspaper size={13} /> 新闻快讯</>}
        action={<button className={button} disabled={busy} onClick={() => postAction("/api/news/refresh", { operator_id: "console" })}>刷新</button>}
      >
        {newsWarnings.length ? (
          <div className="mb-2 rounded-xl border border-[#854d0e] bg-[#241806] p-2 text-[11px] text-[#facc15]">{newsWarnings.join("; ")}</div>
        ) : null}
        <div className="space-y-2">
          {railNewsItems.length ? railNewsItems.map((item, idx) => (
            <div key={idx} className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs">
              <div className="font-medium text-[#e5eefb]">{String(item.title || item.headline || item.summary || "--")}</div>
              <div className="mt-1 text-[11px] text-[#94a3b8]">{String(item.published_at || item.time || item.source || "")}</div>
            </div>
          )) : (
            <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">暂无新闻快讯。</div>
          )}
        </div>
      </Surface>
      <Surface title={<><Power size={13} /> 当前持仓</>}>
        <PositionRailCard position={position} symbol={symbol} latestOrder={orders[0]} />
      </Surface>
    </aside>
  );
}

function DecisionSummary({ data, showSizing = true }: { data: Record<string, unknown>; showSizing?: boolean }) {
  const parts = decisionParts(data);
  const regime = String(decisionValue(parts, ["regime", "regime_candidate", "event_type"]) || "--");
  const direction = String(decisionValue(parts, ["direction"]) || "--");
  const action = String(decisionAction(parts) || "--");
  const confidence = decisionValue(parts, ["confidence"]);
  const sizing = decisionSizing(parts);
  const scoreRows: Array<[string, unknown]> = [
    ["趋势确认", decisionValue(parts, ["trend_confirmation_score", "regime_trend_score"])],
    ["震荡风险", decisionValue(parts, ["range_risk_score", "regime_range_score"])],
    ["新闻风险", decisionValue(parts, ["news_risk_score"])],
    ["订单流确认", decisionValue(parts, ["orderflow_confirmation_score"])],
    ["密集区突破", decisionValue(parts, ["dense_zone_breakout_score"])],
  ];
  return (
    <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
      <Metric label="行情状态" value={regimeLabel(regime)} />
      <Metric label="方向" value={directionLabel(direction)} />
      <Metric label="动作" value={actionLabel(action)} />
      <Metric label="置信度" value={confidencePct(confidence)} />
      <Metric label="仓位档" value={`${sizing.label} / ${sizing.scale}`} />
      {scoreRows.map(([label, value]) => (
        <Metric key={label} label={label} value={confidencePct(value)} tone={Number(value) >= 0.7 ? "good" : Number(value) >= 0.45 ? "warn" : "bad"} />
      ))}
      {showSizing ? <AiSizingTierStrip activeTier={sizing.activeTier} activeScale={sizing.scale} note={sizing.note} /> : null}
    </div>
  );
}

function DecisionRailSummary({ data }: { data: Record<string, unknown> }) {
  const parts = decisionParts(data);
  const regime = String(decisionValue(parts, ["regime", "regime_candidate", "event_type"]) || "--");
  const direction = String(decisionValue(parts, ["direction"]) || "--");
  const action = String(decisionAction(parts) || "--");
  const confidence = decisionValue(parts, ["confidence"]);
  const reason = decisionReason(parts);
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <Metric label="行情" value={regimeLabel(regime)} />
        <Metric label="方向" value={directionLabel(direction)} />
        <Metric label="动作" value={actionLabel(action)} />
        <Metric label="置信度" value={confidencePct(confidence)} />
      </div>
      <div className="grid gap-2 text-xs">
        <BoundaryLine label="消息面" value={alignmentLabel(decisionValue(parts, ["news_alignment"]))} />
        <BoundaryLine label="订单流" value={alignmentLabel(decisionValue(parts, ["orderflow_alignment"]))} />
        <BoundaryLine label="密集区" value={denseZoneLabel(decisionValue(parts, ["dense_zone_position", "current_position"]))} />
      </div>
      <div className="rounded-lg border border-[#263246] bg-[#0b1220] p-2 text-[11px] leading-relaxed text-[#94a3b8]">{reason}</div>
    </div>
  );
}

function AiSizingRail({ data }: { data: Record<string, unknown> }) {
  const parts = decisionParts(data);
  const sizing = decisionSizing(parts);
  const tiers = [
    { key: "block", label: "阻断", scale: "0%" },
    { key: "weak", label: "弱仓", scale: "25%" },
    { key: "normal", label: "标准仓", scale: "50%" },
    { key: "strong", label: "强仓", scale: "75%" },
    { key: "full", label: "满仓", scale: "100%" },
  ];
  return (
    <div className="space-y-2 text-xs">
      <div className="rounded-xl border border-[#60a5fa] bg-[#102a5c] p-3">
        <div className="text-[11px] text-[#bfdbfe]">当前 AI 建议仓位</div>
        <div className={`${mono} mt-1 text-lg font-semibold text-white`}>{sizing.label} / {sizing.scale}</div>
        <div className="mt-2 text-[11px] leading-5 text-[#bfdbfe]">{sizing.note}</div>
      </div>
      <div className="grid gap-1">
        {tiers.map((tier) => {
          const active = tier.key === sizing.activeTier;
          return (
            <div key={tier.key} className={`grid grid-cols-[58px_minmax(0,1fr)_42px] items-center gap-2 rounded-lg border px-2 py-1.5 ${active ? "border-[#60a5fa] bg-[#102a5c]" : "border-[#263246] bg-[#101a2d]"}`}>
              <span className={active ? "font-semibold text-[#93c5fd]" : "text-[#94a3b8]"}>{tier.label}</span>
              <div className="h-1.5 overflow-hidden rounded-full bg-[#1f2a3d]">
                <div className={active ? "h-full rounded-full bg-[#60a5fa]" : "h-full rounded-full bg-[#334155]"} style={{ width: tier.scale }} />
              </div>
              <span className={`${mono} text-right ${active ? "text-[#bfdbfe]" : "text-[#94a3b8]"}`}>{tier.scale}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function PositionRailCard({ position, symbol, latestOrder }: { position: PositionSnapshot | null; symbol: string; latestOrder?: DbRow }) {
  const orderPayload = latestOrder?.payload || {};
  if (!position) {
    return (
      <div className="space-y-2 text-xs">
        <div className="rounded-xl border border-[#14532d] bg-[#052e1a] p-3 text-[#22c55e]">
          <div className="font-semibold">{shortSymbol(symbol)} 空仓</div>
          <div className="mt-1 text-[11px] leading-5 text-[#86efac]">当前没有实盘持仓，等待策略信号与 AI 风控确认。</div>
        </div>
        <BoundaryLine label="最近订单" value={latestOrder ? `${orderStatusLabel(orderPayload.status)} ${sideLabel(orderPayload.side || orderPayload.action)}` : "无记录"} />
      </div>
    );
  }
  return (
    <div className="grid gap-2 text-xs">
      <Metric label="方向" value={positionSideLabel(position.side)} tone={String(position.side).toLowerCase().includes("short") ? "bad" : "good"} />
      <div className="grid grid-cols-2 gap-2">
        <Metric label="数量" value={num(position.qty, 6)} />
        <Metric label="名义价值" value={num(position.notional)} />
        <Metric label="开仓价" value={num(position.entryPrice)} />
        <Metric label="标记价" value={num(position.markPrice)} />
        <Metric label="浮盈亏" value={num(position.pnl)} tone={pnlTone(position.pnl)} />
        <Metric label="止损价" value={num(position.stopLoss)} />
      </div>
      <BoundaryLine label="最近订单" value={latestOrder ? `${orderStatusLabel(orderPayload.status)} ${sideLabel(orderPayload.side || orderPayload.action)}` : "无记录"} />
    </div>
  );
}

function AiSizingTierStrip({ activeTier, activeScale, note }: { activeTier: string; activeScale: string; note: string }) {
  const tiers = [
    { key: "block", label: "阻断", scale: "0%", body: "AI 或硬风控否决，不开仓。" },
    { key: "weak", label: "弱仓", scale: "25%", body: "只允许小仓验证。" },
    { key: "normal", label: "标准仓", scale: "50%", body: "趋势有效但仍需折扣。" },
    { key: "strong", label: "强仓", scale: "75%", body: "多因子同向确认。" },
    { key: "full", label: "满仓", scale: "100%", body: "强趋势且风险项全部通过。" },
  ];
  return (
    <div className="col-span-2 rounded-xl border border-[#263246] bg-gradient-to-br from-[#0b1220] to-[#111827] p-3 text-xs shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] md:col-span-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="font-semibold text-[#e5eefb]">ETH 实盘五档映射</div>
          <div className="mt-1 text-[11px] text-[#94a3b8]">{note}</div>
        </div>
        <span className={`${mono} rounded-full border border-[#60a5fa] bg-[#1d4ed8] px-3 py-1 text-white`}>当前 {tierLabel(activeTier)} / {activeScale}</span>
      </div>
      <div className="grid gap-2 md:grid-cols-5">
        {tiers.map((tier) => {
          const active = tier.key === activeTier;
          return (
            <div key={tier.key} className={`rounded-xl border p-3 transition-all duration-200 ${active ? "border-[#60a5fa] bg-[#102a5c] shadow-[0_12px_28px_rgba(37,99,235,0.22)]" : "border-[#263246] bg-[#101a2d]"}`}>
              <div className="flex items-center justify-between gap-2">
                <span className={`font-semibold ${active ? "text-[#93c5fd]" : "text-[#e5eefb]"}`}>{tier.label}</span>
                <span className={`${mono} text-[11px] ${active ? "text-[#bfdbfe]" : "text-[#94a3b8]"}`}>{tier.scale}</span>
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-[#1f2a3d]">
                <div className={`h-full rounded-full ${active ? "bg-[#60a5fa]" : "bg-[#334155]"}`} style={{ width: tier.scale }} />
              </div>
              <div className="mt-2 min-h-8 text-[11px] leading-4 text-[#94a3b8]">{tier.body}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function DecisionNarrative({ data }: { data: Record<string, unknown> }) {
  const parts = decisionParts(data);
  const rows: Array<[string, string]> = [
    ["消息面", alignmentLabel(decisionValue(parts, ["news_alignment"]))],
    ["订单流", alignmentLabel(decisionValue(parts, ["orderflow_alignment"]))],
    ["密集区", denseZoneLabel(decisionValue(parts, ["dense_zone_position", "current_position"]))],
    ["形态", patternLabel(decisionPatternValue(parts))],
    ["建议", actionLabel(decisionAction(parts))],
  ];
  const reason = decisionReason(parts);
  return (
    <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#cbd5e1]">
      <div className="grid gap-2">
        {rows.map(([label, value]) => (
          <BoundaryLine key={label} label={label} value={value} />
        ))}
      </div>
      <div className="mt-3 rounded-lg border border-[#263246] bg-[#0b1220] p-2 text-[11px] leading-relaxed text-[#94a3b8]">{reason}</div>
    </div>
  );
}

function regimeLabel(value: unknown) {
  const text = String(value || "--");
  const labels: Record<string, string> = {
    trend: "趋势",
    range: "震荡",
    mixed: "混合",
    flat: "横盘",
    price_move: "价格异动",
  };
  return labels[text] || text;
}

function directionLabel(value: unknown) {
  const text = String(value || "--");
  const labels: Record<string, string> = {
    long: "偏多",
    short: "偏空",
    flat: "观望",
    neutral: "中性",
  };
  return labels[text] || text;
}

function actionLabel(value: unknown) {
  const text = String(value || "--");
  const labels: Record<string, string> = {
    hold: "等待",
    block: "阻断",
    reduce: "减仓",
    allow: "允许",
    confirm: "确认",
    wait: "等待",
  };
  return labels[text] || text;
}

function alignmentLabel(value: unknown) {
  const text = String(value || "--");
  const labels: Record<string, string> = {
    aligned: "同向",
    conflict: "冲突",
    neutral: "中性",
    bullish: "利多",
    bearish: "利空",
  };
  return labels[text] || text;
}

function denseZoneLabel(value: unknown) {
  const text = String(value || "--");
  const labels: Record<string, string> = {
    inside_value: "密集区内部",
    inside_value_above_mid: "密集区内偏强",
    inside_value_below_mid: "密集区内偏弱",
    inside_zone: "密集区内部",
    inside_zone_near_resistance: "密集区内接近阻力",
    inside_zone_near_support: "密集区内接近支撑",
    near_resistance: "接近阻力",
    near_support: "接近支撑",
    vacuum_breakout: "真空区突破",
    unknown: "未知",
  };
  return labels[text] || text;
}

function patternLabel(value: unknown) {
  const text = String(value || "--");
  const labels: Record<string, string> = {
    trend: "趋势结构",
    range: "震荡结构",
    breakout: "突破结构",
    range_rectangle: "矩形震荡",
    symmetrical_triangle: "收敛三角形",
    ascending_triangle: "上升三角形",
    descending_triangle: "下降三角形",
    falling_wedge: "下降楔形",
    rising_wedge: "上升楔形",
    rectangle_breakout: "矩形突破",
    generic_breakout: "未分类突破",
    ascending_channel: "上升通道",
    descending_channel: "下降通道",
    bull_flag: "多头旗形",
    bear_flag: "空头旗形",
    double_top: "双顶",
    double_bottom: "双底",
    head_shoulders: "头肩顶",
    inverse_head_shoulders: "头肩底",
    compression: "收敛压缩",
    channel: "通道结构",
    reversal: "反转结构",
    continuation: "延续结构",
    range_rotation: "区间轮动",
    unknown: "未知",
  };
  return labels[text] || text;
}

function MiniDenseZone({ denseZone }: { denseZone?: DenseZonePayload }) {
  if (!denseZone) {
    return <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs text-[#94a3b8]">密集区：等待本地结构分析。</div>;
  }
  return (
    <div className="rounded-xl border border-[#263246] bg-[#101a2d] p-3 text-xs">
      <div className="mb-2 font-semibold text-[#e5eefb]">本地密集区</div>
      <div className="grid gap-2">
        <BoundaryLine label="上沿" value={num(denseZone.zone_high ?? denseZone.vah)} />
        <BoundaryLine label="POC/中位" value={num(denseZone.zone_mid ?? denseZone.poc)} />
        <BoundaryLine label="下沿" value={num(denseZone.zone_low ?? denseZone.val)} />
        <BoundaryLine label="结构" value={String(denseZone.structure_label || breakoutStatusLabel(denseZone.breakout_status))} />
      </div>
    </div>
  );
}

function confidencePct(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${Math.round(number * 100)}%`;
}

function confidenceBarWidth(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "0%";
  return `${Math.max(0, Math.min(100, Math.round(number * 100)))}%`;
}

function aiScaleFromConfidence(value: unknown) {
  const confidence = Number(value);
  if (!Number.isFinite(confidence) || confidence < 0.55) return { label: "阻断", scale: "0%" };
  if (confidence >= 0.85) return { label: "满仓", scale: "100%" };
  if (confidence >= 0.75) return { label: "强仓", scale: "75%" };
  if (confidence >= 0.65) return { label: "标准仓", scale: "50%" };
  return { label: "弱仓", scale: "25%" };
}

function tierKeyFromConfidence(value: unknown) {
  const confidence = Number(value);
  if (!Number.isFinite(confidence) || confidence < 0.55) return "block";
  if (confidence >= 0.85) return "full";
  if (confidence >= 0.75) return "strong";
  if (confidence >= 0.65) return "normal";
  return "weak";
}

function tierLabel(value: unknown) {
  const text = String(value || "block");
  const labels: Record<string, string> = {
    full: "满仓",
    strong: "强仓",
    normal: "标准仓",
    weak: "弱仓",
    block: "阻断",
  };
  return labels[text] || text;
}

function positionScaleLabel(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "0%";
  return `${Math.max(0, Math.min(100, Math.round(number * 100)))}%`;
}

function breakoutStatusLabel(value: unknown) {
  const text = String(value || "--");
  const labels: Record<string, string> = {
    inside_zone: "密集区内部",
    breakout_up: "向上突破",
    breakout_down: "向下突破",
    retest_support: "回踩支撑",
    retest_resistance: "反抽阻力",
    failed_breakout: "假突破",
    vacuum_travel: "真空区迁移",
    unknown: "未知",
  };
  return labels[text] || text;
}

export function AppError({ error }: { error: string }) {
  return (
    <div className="flex h-screen items-center justify-center bg-[#07111f] text-[#fb7185]">
      <AlertTriangle className="mr-2" />
      {error}
    </div>
  );
}

