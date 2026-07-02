export type SymbolConfig = { symbol: string; timeframe: string; leverage: number };

export type DbRow<T = Record<string, unknown>> = {
  id: number;
  created_at: string;
  symbol: string | null;
  payload: T;
};

export type StatusResponse = {
  mode: string;
  execution_mode?: "mock" | "live";
  opening_paused: boolean;
  trade_mode: string;
  enabled_symbols?: string[];
  report_symbols?: string[];
  risk: Record<string, unknown>;
  ai: Record<string, unknown>;
  symbols: SymbolConfig[];
  latest_decisions?: Record<string, DbRow | null>;
  exchange_safety?: DbRow | null;
  latest_order_lifecycle?: DbRow | null;
  latest_data_health?: DbRow | null;
  latest_ai_drift?: DbRow | null;
  latest_news_risk_review?: DbRow | null;
  latest_position_review?: DbRow | null;
  latest_ai_budget?: DbRow | null;
  latest_worker_heartbeats?: Record<string, DbRow | null>;
  latest_maintenance?: DbRow | null;
};

export type StrategyProfile = {
  symbol: string;
  profile_name: string;
  strategy_type: string;
  enabled: boolean;
  opening_authorized: boolean;
  report_enabled: boolean;
  live_ready: boolean;
  notes: string;
  params: Record<string, unknown>;
  backtest_defaults?: Record<string, number | string | boolean>;
  optimization_defaults?: Record<string, unknown>;
  execution_contract?: Record<string, unknown>;
};

export type StrategyChannel = {
  channel: "trend" | "follower" | "range";
  label: string;
  strategy_type: "trend" | "trend_follower" | "range_reserved";
  account_slot: "trend" | "follower" | "range";
  account_label: string;
  enabled: boolean;
  executable: boolean;
  status: string;
  mode: "mock" | "live";
  opening_paused: boolean;
  authorized_symbols: string[];
  configured_symbols: string[];
  account_configured: boolean;
  gateway_binding: string;
  live_ready: boolean;
  ai_sizing_tiers: Array<Record<string, number | string>>;
  notes: string[];
};

export type PlatformOverview = {
  platform: {
    shell: string;
    core: string;
    execution_mode: "mock" | "live";
    trade_mode: string;
    notification_channels: string[];
    agent_gateway?: {
      enabled: boolean;
      version: string;
      scopes: string[];
      paper_only: boolean;
      live_trading: string;
    };
  };
  workspaces: Array<{ id: WorkspaceId; label: string }>;
  strategy_channels?: StrategyChannel[];
  strategy_profiles: StrategyProfile[];
  latest_backtest_runs: DbRow[];
  latest_ai_review_runs: DbRow[];
};

export type ReadinessCheck = {
  id: string;
  label: string;
  status: "ok" | "warn" | "block";
  detail: string;
  age_minutes?: number | null;
};

export type SystemReadiness = {
  overall: "ok" | "warn" | "block";
  execution_mode: "mock" | "live";
  trade_mode: string;
  configured_symbols: string[];
  enabled_symbols: string[];
  profile_count: number;
  enabled_profile_count: number;
  authorized_profile_count: number;
  live_ready_profile_count: number;
  deepseek_ready: boolean;
  exchange_safety?: DbRow | null;
  latest_reconciliation?: DbRow | null;
  latest_order_lifecycle?: DbRow | null;
  latest_data_health?: DbRow | null;
  latest_ai_drift?: DbRow | null;
  latest_news_risk_review?: DbRow | null;
  latest_ai_budget?: DbRow | null;
  latest_worker_heartbeats?: Record<string, DbRow | null>;
  worker_heartbeat_details?: WorkerHeartbeatDetail[];
  latest_maintenance?: DbRow | null;
  runtime_alerts?: RuntimeAlert[];
  runtime_alert_summary?: RuntimeAlertSummary;
  checks: ReadinessCheck[];
};

export type RuntimeAlert = {
  event: string;
  level: "info" | "warn" | "critical";
  source: string;
  message: string;
  execution_mode?: string;
  created_at?: string;
  payload?: Record<string, unknown>;
};

export type RuntimeAlertSummary = {
  total: number;
  critical: number;
  warn: number;
  status: "ok" | "warn" | "block";
};

export type WorkerHeartbeatDetail = {
  worker: string;
  status: string;
  reason: string;
  age_seconds?: number | null;
  allowed_seconds?: number | null;
  checked_at?: string | null;
  last_success_at?: string | null;
  row_id?: number | null;
};

export type ExecutionAccountSlot = {
  slot: "trend" | "follower" | "range";
  label: string;
  exchange: string;
  strategy_type: "trend" | "trend_follower" | "range_reserved";
  configured: boolean;
  version: number;
  key_tail: string;
  secret_tail: string;
  gateway_binding: string;
  live_routing: string;
  credential_source?: string;
  max_leverage?: number;
};

export type ConsoleSession = {
  ok: boolean;
  auth_required: boolean;
  auth_configured?: boolean;
  authenticated: boolean;
  user: null | {
    username: string;
    role: "admin" | "account1" | "account2" | "range";
    label: string;
    account_slot: "trend" | "follower" | "range" | null;
    visible_account_slots: Array<"trend" | "follower" | "range">;
    capabilities: {
      manage_runtime: boolean;
      manage_strategy_parameters: boolean;
      manage_position_review?: boolean;
      manage_api_keys: boolean;
      execute_manual_orders: boolean;
      edit_own_leverage: boolean;
      view_all_accounts: boolean;
    };
  };
};

export type WorkspaceId = "dashboard" | "market" | "strategy" | "ai" | "agent" | "execution" | "data";

export type MarketSymbolsResponse = {
  items: Array<{ symbol: string; base: string; quote: string; configured: boolean; strategy_enabled?: boolean }>;
};

export type Candle = { time: string; open: number; high: number; low: number; close: number; volume: number };
export type CandleResponse = { items: Candle[]; source?: string; warning?: string; closed_only?: boolean };

export type MarketTickerResponse = {
  symbol: string;
  source: string;
  requested_source?: string;
  last?: number | null;
  bid?: number | null;
  ask?: number | null;
  mark?: number | null;
  index?: number | null;
  timestamp?: string;
  warning?: string;
};
export type ApiList<T = Record<string, unknown>> = {
  ok?: boolean;
  items: Array<DbRow<T>>;
  account_slot?: string | null;
  source?: string;
  cached?: boolean;
  stale?: boolean;
  error_type?: string;
  message?: string;
};
export type NewsResponse = ApiList & {
  ok?: boolean;
  source?: string;
  source_status?: "fresh" | "stale" | "refresh_failed" | string;
  refreshed?: boolean;
  items_count?: number;
  digest_summary?: string;
  timeline?: Array<Record<string, unknown>>;
  warnings?: string[];
  age_minutes?: number;
  stale?: boolean;
  generated_at?: string | null;
  summary?: string;
};

export type AiPositionTierStats = {
  entries?: number;
  closed?: number;
  open?: number;
  wins?: number;
  losses?: number;
  win_rate?: number | null;
  total_actual_pnl_usdt?: number;
  total_baseline_pnl_usdt?: number;
  total_ai_delta_pnl_usdt?: number;
  winner_upside_missed_usdt?: number;
  loser_loss_saved_usdt?: number;
  winner_extra_profit_usdt?: number;
  loser_extra_loss_usdt?: number;
  avg_position_scale?: number | null;
  avg_decision_score?: number | null;
  avg_ai_confidence?: number | null;
};

export type AiPositionShadowTierStats = {
  closed?: number;
  wins?: number;
  losses?: number;
  win_rate?: number | null;
  total_pnl_usdt?: number;
  avg_pnl_usdt?: number | null;
  scale?: number;
};

export type AiPositionTierAudit = {
  ok?: boolean;
  symbol?: string | null;
  account_slot?: string | null;
  sample_warning?: boolean;
  min_closed_trades_for_reliable_read?: number;
  overall?: AiPositionTierStats;
  by_tier?: Record<string, AiPositionTierStats>;
  shadow_by_tier?: Record<string, AiPositionShadowTierStats>;
  trades?: Array<Record<string, unknown>>;
};

export type DenseZonePayload = {
  symbol?: string;
  poc?: number;
  vah?: number;
  val?: number;
  support?: number | null;
  resistance?: number | null;
  current_position?: string;
  strength?: number;
  zone_low?: number | null;
  zone_high?: number | null;
  zone_mid?: number | null;
  previous_zone_low?: number | null;
  previous_zone_high?: number | null;
  next_zone_low?: number | null;
  next_zone_high?: number | null;
  vacuum_low?: number | null;
  vacuum_high?: number | null;
  breakout_status?: string;
  retest_status?: string;
  trend_score?: number;
  range_score?: number;
  structure_label?: string;
};

export type BacktestTrade = {
  side?: string;
  entry_time?: string;
  exit_time?: string;
  entry_price?: number;
  exit_price?: number;
  qty?: number;
  pnl?: number;
  return_pct?: number;
  fee_paid?: number;
  slippage_paid?: number;
  funding_paid?: number;
  requested_qty?: number;
  filled_qty?: number;
  fill_ratio?: number;
  holding_bars?: number;
  exit_reason?: string;
  stop_loss_price?: number;
  max_adverse_excursion?: number;
  max_adverse_excursion_pct?: number;
  intrabar_path?: string;
};

export type BacktestResult = {
  total_return_pct?: number;
  max_drawdown_pct?: number;
  trade_count?: number;
  win_rate_pct?: number;
  profit_factor?: number;
  leverage?: number;
  final_equity?: number;
  cost_model?: {
    cost_pct_of_initial_equity?: number;
    total_fee_paid?: number;
    total_slippage_paid?: number;
    total_funding_paid?: number;
    total_cost_paid?: number;
    funding_rate_per_8h?: number;
    min_order_qty?: number;
    max_volume_participation?: number;
  };
  skipped_orders?: Array<Record<string, number | string | boolean | null>>;
  trades?: BacktestTrade[];
  trade_ledger?: BacktestTrade[];
  ai_guard_applied?: boolean;
  raw_ai_proxy?: BacktestResult;
};

export type BacktestJob = {
  status?: string;
  progress?: number;
  error?: string;
  message?: string;
  result?: BacktestResult | OptimizationResult;
};

export type OptimizationCandidate = {
  params?: Record<string, number | string | boolean>;
  score?: number;
  train?: Record<string, number>;
  validation?: Record<string, number>;
  warnings?: string[];
};

export type OptimizationResult = {
  baseline?: Record<string, number>;
  baseline_params?: Record<string, number | string | boolean>;
  best?: OptimizationCandidate;
  candidates?: OptimizationCandidate[];
  searched_candidates?: number;
  selection_policy?: string;
  walk_forward_proposal_id?: number;
  walk_forward_acceptance?: WalkForwardAcceptance;
};

export type WalkForwardAcceptance = {
  accepted?: boolean;
  status?: "needs_review" | "rejected" | string;
  reasons?: string[];
  risks?: string[];
  metrics?: Record<string, number>;
  thresholds?: Record<string, number | boolean>;
};

export type WalkForwardProposalPayload = {
  type?: "walk_forward_parameter_proposal" | string;
  status?: "needs_review" | "rejected" | string;
  symbol?: string;
  timeframe?: string;
  job_id?: string;
  summary?: string;
  baseline?: Record<string, number>;
  baseline_params?: Record<string, number | string | boolean>;
  best?: OptimizationCandidate;
  candidates?: OptimizationCandidate[];
  data_split?: Record<string, number>;
  acceptance?: WalkForwardAcceptance;
  proposed_changes?: Record<string, { old?: number | string | boolean | null; new?: number | string | boolean | null }>;
  changes?: Record<string, unknown>;
  source?: string;
  auto_apply?: boolean;
  risk_note?: string;
};
