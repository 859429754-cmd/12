import { expect, type Page, test } from "@playwright/test";

type ConsoleUser = "admin" | "account1" | "account2";

const now = "2026-07-04T12:00:00.000Z";

function session(user: ConsoleUser | null) {
  if (!user) {
    return { ok: false, auth_required: true, authenticated: false, user: null };
  }
  const users = {
    admin: {
      username: "admin",
      role: "admin",
      label: "管理员",
      account_slot: null,
      visible_account_slots: ["trend", "follower", "range"],
      capabilities: {
        manage_runtime: true,
        manage_strategy_parameters: true,
        manage_position_review: true,
        manage_api_keys: true,
        execute_manual_orders: true,
        edit_own_leverage: true,
        view_all_accounts: true,
      },
    },
    account1: {
      username: "account1",
      role: "account1",
      label: "账号1",
      account_slot: "trend",
      visible_account_slots: ["trend"],
      capabilities: {
        manage_runtime: false,
        manage_strategy_parameters: false,
        manage_position_review: false,
        manage_api_keys: false,
        execute_manual_orders: false,
        edit_own_leverage: true,
        view_all_accounts: false,
      },
    },
    account2: {
      username: "account2",
      role: "account2",
      label: "账号2",
      account_slot: "follower",
      visible_account_slots: ["follower"],
      capabilities: {
        manage_runtime: false,
        manage_strategy_parameters: false,
        manage_position_review: false,
        manage_api_keys: false,
        execute_manual_orders: false,
        edit_own_leverage: true,
        view_all_accounts: false,
      },
    },
  } as const;
  return {
    ok: true,
    auth_required: true,
    authenticated: true,
    session_expires_at: "2026-07-05T00:00:00.000Z",
    session_seconds_remaining: 43200,
    session_expiring_soon: false,
    user: users[user],
  };
}

function dbRow(payload: Record<string, unknown>, symbol: string | null = "ETH/USDT:USDT") {
  return { id: 1, created_at: now, symbol, payload };
}

async function mockConsoleApi(page: Page, options: { executionMode?: "mock" | "live" } = {}) {
  let activeUser: ConsoleUser | null = null;
  const requests: string[] = [];
  const actionPosts: { path: string; body: Record<string, unknown> }[] = [];
  const executionMode = options.executionMode || "live";
  page.on("request", (request) => requests.push(request.url()));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const accountSlot = url.searchParams.get("account_slot") || (activeUser === "account2" ? "follower" : "trend");
    const json = async (body: unknown, status = 200) => {
      await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    };

    if (path === "/api/auth/session") {
      return json(session(activeUser), activeUser ? 200 : 401);
    }
    if (path === "/api/auth/login") {
      const body = JSON.parse(request.postData() || "{}");
      activeUser = body.username === "account2" ? "account2" : body.username === "admin" ? "admin" : "account1";
      return json(session(activeUser));
    }
    if (path === "/api/auth/logout") {
      activeUser = null;
      return json({ ok: true, authenticated: false });
    }
    if (path === "/api/status") {
      return json({
        mode: "running",
        execution_mode: executionMode,
        opening_paused: false,
        trade_mode: executionMode,
        enabled_symbols: ["ETH/USDT:USDT"],
        report_symbols: ["ETH/USDT:USDT"],
        risk: { max_total_leverage: 4, ai_sizing_policy: "hybrid_subjective_guarded_v2" },
        ai: { enabled: true },
        symbols: [{ symbol: "ETH/USDT:USDT", timeframe: "1h", leverage: 4 }],
        latest_decisions: {
          "ETH/USDT:USDT": dbRow({
            action_suggestion: "hold",
            confidence: 0.62,
            direction: "long",
            regime: "trend",
            signal: { action: "long", technical_evidence: { pattern: "上升通道", dense_zone_position: "above_value" } },
            risk: { position_tier: "strong", position_scale: 0.75 },
          }),
        },
        latest_order_lifecycle: dbRow({ status: "unknown", client_order_id: "aiq_unknown_12345678", account_slot: "trend", order_type: "market" }),
      });
    }
    if (path === "/api/platform/overview") {
      return json({
        platform: { shell: "console", core: "ai_quant_trader", execution_mode: executionMode, trade_mode: executionMode, notification_channels: [] },
        workspaces: [
          { id: "dashboard", label: "总览" },
          { id: "market", label: "行情图表" },
          { id: "strategy", label: "策略与回测" },
          { id: "ai", label: "AI 大脑" },
          { id: "agent", label: "智能体网关" },
          { id: "execution", label: "交易执行" },
          { id: "data", label: "快讯与数据" },
        ],
        strategy_profiles: [
          { symbol: "ETH/USDT:USDT", profile_name: "ETH 趋势策略", strategy_type: "trend", enabled: true, opening_authorized: true, report_enabled: true, live_ready: true, notes: "", params: {} },
        ],
        strategy_channels: [
          { channel: "trend", label: "趋势", strategy_type: "trend", account_slot: "trend", account_label: "账号1", enabled: true, executable: true, status: "running", mode: executionMode, opening_paused: false, authorized_symbols: ["ETH/USDT:USDT"], configured_symbols: ["ETH/USDT:USDT"], account_configured: true, gateway_binding: "trend", live_ready: true, ai_sizing_tiers: [], notes: [] },
          { channel: "follower", label: "跟随", strategy_type: "trend_follower", account_slot: "follower", account_label: "账号2", enabled: true, executable: true, status: "running", mode: executionMode, opening_paused: false, authorized_symbols: ["ETH/USDT:USDT"], configured_symbols: ["ETH/USDT:USDT"], account_configured: true, gateway_binding: "follower", live_ready: true, ai_sizing_tiers: [], notes: [] },
        ],
        latest_backtest_runs: [],
        latest_ai_review_runs: [],
      });
    }
    if (path === "/api/system/readiness") {
      return json({
        overall: "block",
        execution_mode: executionMode,
        trade_mode: executionMode,
        configured_symbols: ["ETH/USDT:USDT"],
        enabled_symbols: ["ETH/USDT:USDT"],
        profile_count: 1,
        enabled_profile_count: 1,
        authorized_profile_count: 1,
        live_ready_profile_count: 1,
        deepseek_ready: true,
        exchange_safety: dbRow({ status: "reconciliation_required", can_open_new_entries: false, reason: "exchange_reconciliation_required" }, null),
        latest_reconciliation: dbRow({ status: "reconciliation_required" }, null),
        latest_order_lifecycle: dbRow({ status: "unknown", client_order_id: "aiq_unknown_12345678", account_slot: "trend", order_type: "market" }, null),
        unresolved_order_lifecycle: [
          {
            id: 7,
            created_at: now,
            symbol: "ETH/USDT:USDT",
            client_order_id: "aiq_unknown_12345678",
            status: "unknown",
            order_type: "market",
            account_slot: "trend",
            reason: "submit_timeout",
            error_type: "TimeoutError",
          },
        ],
        latest_data_health: dbRow({ status: "ok" }, null),
        latest_ai_drift: dbRow({ status: "ok" }, null),
        latest_news_risk_review: dbRow({ risk: { reason: "neutral" }, event: { title: "市场新闻审计" } }, null),
        latest_ai_budget: dbRow({ status: "ok" }, null),
        latest_worker_heartbeats: {},
        worker_heartbeat_details: [],
        latest_maintenance: dbRow({ status: "ok" }, null),
        runtime_alerts: [],
        runtime_alert_summary: { total: 0, critical: 0, warn: 0, status: "ok" },
        checks: [
          { id: "exchange_safety", label: "Exchange safety", status: "block", detail: "Exchange private state is not verified." },
          { id: "order_lifecycle", label: "Order lifecycle", status: "block", detail: "1 unresolved order lifecycle issue(s) require operator review." },
          { id: "news", label: "News cache", status: "ok", detail: "Latest news cache was updated less than 1 minute ago." },
        ],
      });
    }
    if (path === "/api/market/candles") {
      const base = 1700;
      return json({
        source: "fixture",
        items: Array.from({ length: 180 }, (_, idx) => {
          const close = base + idx * 0.6 + Math.sin(idx / 8) * 12;
          return { time: new Date(Date.UTC(2026, 6, 1, idx)).toISOString(), open: close - 2, high: close + 8, low: close - 8, close, volume: 1500 + idx };
        }),
      });
    }
    if (path === "/api/market/ticker") {
      return json({ symbol: "ETH/USDT:USDT", source: "fixture", last: 1808.5, mark: 1808.2, timestamp: now });
    }
    if (path === "/api/markets/symbols") {
      return json({ items: [{ symbol: "ETH/USDT:USDT", base: "ETH", quote: "USDT", configured: true, strategy_enabled: true }] });
    }
    if (path === "/api/account/balance") {
      const follower = accountSlot === "follower";
      return json({ ok: true, account_slot: accountSlot, balance_source: "gate_live_readonly", usdt_total: follower ? 2222 : 1111, usdt_free: follower ? 2200 : 1000, usdt_used: follower ? 22 : 111 });
    }
    if (path === "/api/positions") {
      const follower = accountSlot === "follower";
      return json({
        ok: true,
        account_slot: accountSlot,
        items: follower ? [] : [dbRow({ symbol: "ETH/USDT:USDT", side: "long", qty: 0.2, entry_price: 1700, mark_price: 1808, unrealized_pnl: 21.6, notional: 361.6 })],
      });
    }
    if (path === "/api/news/latest") {
      return json({
        ok: true,
        source: "news_cache",
        source_status: "fresh",
        items_count: 2,
        age_minutes: 0.3,
        stale: false,
        items: [
          dbRow({ title: "金十数据：美联储官员称风险资产波动上升", source: "金十数据", credibility: 0.91, important: true, published_at: now }, null),
          dbRow({ title: "BTC 带动 ETH 盘中反弹", source: "全网数据", credibility: 0.82, published_at: now }, null),
        ],
      });
    }
    if (path === "/api/execution/accounts") {
      return json({ items: [
        { slot: "trend", label: "账号1", exchange: "gate", strategy_type: "trend", configured: true, version: 1, key_tail: "1111", secret_tail: "1111", gateway_binding: "trend", live_routing: "live", max_leverage: 4 },
        { slot: "follower", label: "账号2", exchange: "gate", strategy_type: "trend_follower", configured: true, version: 1, key_tail: "2222", secret_tail: "2222", gateway_binding: "follower", live_routing: "live", max_leverage: 4 },
      ] });
    }
    if (path === "/api/orders" || path === "/api/order-lifecycle" || path === "/api/decisions") {
      return json({ ok: true, items: [dbRow({ status: "unknown", client_order_id: "aiq_unknown_12345678", account_slot: accountSlot, order_type: "market" })] });
    }
    if (path === "/api/audits/ai-position-tiers") {
      return json({ ok: true, account_slot: accountSlot, sample_warning: "", tiers: [], summary: {} });
    }
    if (path === "/api/dense-zones/latest") {
      return json({ item: dbRow({ current_position: "above_value", poc: 1700, vah: 1780, val: 1650 }) });
    }
    if (path === "/api/risk/summary") {
      return json({ ok: true, max_total_leverage: 4 });
    }
    if (path === "/api/system/security-events") {
      return json({ items: [], summary: { total: 0, by_event: {}, latest_created_at: null } });
    }
    if (path === "/api/news/refresh") {
      return json({ ok: true, refreshed: true });
    }
    if (path.startsWith("/api/control/")) {
      actionPosts.push({ path, body: JSON.parse(request.postData() || "{}") });
      return json({ ok: true, path });
    }
    return json({ ok: true, items: [] });
  });
  return { requests, actionPosts };
}

test("账号登录后总览显示新闻、图表和未解决订单事故", async ({ page }) => {
  const { requests } = await mockConsoleApi(page);
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  await page.goto("/");
  await expect(page.getByText("AI 量化控制台登录")).toBeVisible();
  await page.getByPlaceholder("用户名").fill("account1");
  await page.getByPlaceholder("密码").fill("yx");
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByText("当前登录")).toBeVisible();
  await expect(page.getByText("账号1").first()).toBeVisible();
  await expect(page.getByText("ETH 趋势策略").first()).toBeVisible();
  await expect(page.getByText("金十数据：美联储官员称风险资产波动上升").first()).toBeVisible();
  await expect(page.getByText("未解决订单生命周期事故").first()).toBeVisible();
  await expect(page.getByText(/trend \/ market \/ 未知 \/ client #12345678/).first()).toBeVisible();
  await page.getByRole("button", { name: "行情图表" }).click();
  await expect(page.locator("canvas").first()).toBeVisible();
  await page.getByRole("button", { name: "总览" }).click();
  await page.getByRole("button", { name: "刷新" }).first().click();
  await expect.poll(() => requests.some((url) => url.includes("/api/account/balance?account_slot=trend"))).toBeTruthy();
  await expect.poll(() => requests.some((url) => url.includes("/api/news/refresh"))).toBeTruthy();
  expect(consoleErrors.filter((item) => !item.includes("401 (Unauthorized)"))).toEqual([]);
});

test("账号2登录后只读取 follower 余额和持仓", async ({ page }) => {
  const { requests } = await mockConsoleApi(page);
  await page.goto("/");
  await page.getByPlaceholder("用户名").fill("account2");
  await page.getByPlaceholder("密码").fill("wx");
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByText("账号2").first()).toBeVisible();
  await expect(page.getByText("2,222").first()).toBeVisible();
  await expect(page.getByText("ETH 空仓").first()).toBeVisible();
  await expect.poll(() => requests.some((url) => url.includes("/api/account/balance?account_slot=follower"))).toBeTruthy();
  await expect.poll(() => requests.some((url) => url.includes("/api/positions?limit=50&account_slot=follower"))).toBeTruthy();
  await expect.poll(() => requests.some((url) => url.includes("/api/order-lifecycle?limit=120") && url.includes("account_slot=follower"))).toBeTruthy();
  expect(requests.some((url) => url.includes("/api/account/balance?account_slot=trend"))).toBeFalsy();
});

test("管理员开仓授权走账号权限而不是 Trade PIN", async ({ page }) => {
  const { requests } = await mockConsoleApi(page);
  page.on("dialog", (dialog) => dialog.accept());

  await page.goto("/");
  await page.getByPlaceholder("用户名").fill("admin");
  await page.getByPlaceholder("密码").fill("1234567");
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByText("管理员").first()).toBeVisible();
  await expect(page.getByText("Trade PIN")).toHaveCount(0);
  await page.getByRole("button", { name: "授权开仓" }).first().click();
  await expect.poll(() => requests.some((url) => url.includes("/api/control/authorize"))).toBeTruthy();
});

test("普通账号不能切换实盘或提交管理控制", async ({ page }) => {
  const { actionPosts } = await mockConsoleApi(page, { executionMode: "mock" });
  await page.goto("/");
  await page.getByPlaceholder("用户名").fill("account1");
  await page.getByPlaceholder("密码").fill("yx");
  await page.getByRole("button", { name: "登录" }).click();

  await page.getByRole("button", { name: "交易执行" }).click();
  await expect(page.getByText("当前账号只能查看交易链路和修改自己账户的杠杆上限").first()).toBeVisible();
  await expect(page.getByText("当前权限：只读账户").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "开启实盘" }).first()).toBeDisabled();
  await expect(page.getByRole("button", { name: /平仓 ETH/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "暂停开仓并一键全平" })).toHaveCount(0);
  expect(actionPosts).toEqual([]);
});

test("管理员切换实盘必须提交二次确认字段", async ({ page }) => {
  const { actionPosts } = await mockConsoleApi(page, { executionMode: "mock" });
  page.on("dialog", (dialog) => dialog.accept());

  await page.goto("/");
  await page.getByPlaceholder("用户名").fill("admin");
  await page.getByPlaceholder("密码").fill("1234567");
  await page.getByRole("button", { name: "登录" }).click();

  await page.getByRole("button", { name: "交易执行" }).click();
  await page.getByRole("button", { name: "开启实盘" }).first().click();

  await expect.poll(() => actionPosts.some((item) => item.path === "/api/control/runtime-mode")).toBeTruthy();
  expect(actionPosts.find((item) => item.path === "/api/control/runtime-mode")?.body).toMatchObject({
    operator_id: "console",
    dry_run: false,
    confirm_admin_action: true,
  });
});
