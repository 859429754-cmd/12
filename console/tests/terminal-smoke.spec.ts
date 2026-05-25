import { expect, test } from "@playwright/test";

const baseURL = process.env.CONSOLE_URL || "http://127.0.0.1:8090";

test("控制台加载动态标的、图表和基础控制请求", async ({ page }) => {
  const requests: string[] = [];
  const consoleErrors: string[] = [];
  page.on("request", (request) => requests.push(request.url()));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  await page.route("**/api/market/candles**", async (route) => {
    const base = 2100;
    const items = Array.from({ length: 180 }, (_, idx) => {
      const close = base + Math.sin(idx / 8) * 45 + idx * 0.9;
      const open = close - Math.sin(idx / 5) * 8;
      return {
        time: new Date(Date.UTC(2026, 4, 10, idx)).toISOString(),
        open,
        high: Math.max(open, close) + 12,
        low: Math.min(open, close) - 12,
        close,
        volume: 1200 + idx * 18,
      };
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ source: "fixture", warning: "fixture candles", items }),
    });
  });

  await page.goto(baseURL, { waitUntil: "networkidle" });

  await expect(page.locator("main")).toHaveCSS("background-color", "rgb(13, 17, 23)");
  await expect(page.getByText("专业图表", { exact: true })).toBeVisible();
  await expect(page.locator("canvas").first()).toBeVisible();
  await expect(page.getByText(/区间高/)).toBeVisible();
  await expect(page.getByText(/区间低/)).toBeVisible();
  await expect(page.getByRole("button", { name: "TradingView" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "EMA89" })).toBeVisible();
  await expect(page.getByRole("button", { name: "KC(20,2.8)" })).toBeVisible();
  await expect(page.getByRole("button", { name: "成交量" })).toBeVisible();
  await expect(page.getByRole("button", { name: "策略信号" })).toBeVisible();
  await expect(page.getByRole("button", { name: "AI决策" })).toBeVisible();
  await expect(page.getByText(/新闻已更新|新闻缓存过期|等待刷新/)).toBeVisible();

  const symbolOptions = await page.locator("select").first().locator("option").allTextContents();
  expect(symbolOptions.join(" ")).not.toContain("ARB");
  expect(symbolOptions.join(" ")).not.toContain("OP");
  await expect(page.getByRole("option", { name: "免费备份" })).toHaveCount(1);

  await page.locator("button[title='刷新']").click();
  await page.getByRole("button", { name: "1D" }).click();
  await page.getByRole("button", { name: "KC(20,2.8)" }).click();
  expect(requests.some((url) => url.includes("limit=10000"))).toBeTruthy();
  await expect.poll(() => requests.some((url) => url.includes("/api/status"))).toBeTruthy();
  expect(consoleErrors).toEqual([]);
});

test("模拟切真实必须打开 Trade PIN 确认框且可取消", async ({ page }) => {
  await page.goto(baseURL, { waitUntil: "networkidle" });

  await page.getByRole("button", { name: "模拟运行" }).click();
  await expect(page.getByText("切换真实运行确认")).toBeVisible();
  await expect(page.getByPlaceholder("输入 Trade PIN")).toBeVisible();

  await page.getByRole("button", { name: "取消" }).click();
  await expect(page.getByText("切换真实运行确认")).toHaveCount(0);
});
