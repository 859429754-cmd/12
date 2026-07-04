import { expect, test, type Page } from "@playwright/test";

const cloudUrl = process.env.CONSOLE_URL;

const credentials = {
  account1: {
    username: process.env.AIQUANT_E2E_ACCOUNT1_USER || "account1",
    password: process.env.AIQUANT_E2E_ACCOUNT1_PASSWORD || "",
    label: "账号1",
  },
  account2: {
    username: process.env.AIQUANT_E2E_ACCOUNT2_USER || "account2",
    password: process.env.AIQUANT_E2E_ACCOUNT2_PASSWORD || "",
    label: "账号2",
  },
  admin: {
    username: process.env.AIQUANT_E2E_ADMIN_USER || "admin",
    password: process.env.AIQUANT_E2E_ADMIN_PASSWORD || "",
    label: "管理员",
  },
};

function missingCloudReadonlyConfig() {
  if (!cloudUrl) return "CONSOLE_URL is not configured";
  const missing = Object.entries(credentials)
    .filter(([, item]) => !item.password)
    .map(([name]) => `AIQUANT_E2E_${name.toUpperCase()}_PASSWORD`);
  return missing.length ? `missing ${missing.join(", ")}` : "";
}

async function login(page: Page, username: string, password: string) {
  await page.goto("/");
  await expect(page.getByText("AI 量化控制台登录")).toBeVisible();
  await page.getByPlaceholder("用户名").fill(username);
  await page.getByPlaceholder("密码").fill(password);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByText("当前登录")).toBeVisible();
}

async function switchToMobile(page: Page, label: string) {
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator("header").getByText(label)).toBeVisible();
}

test.describe("真实云端控制台只读 smoke", () => {
  test.skip(Boolean(missingCloudReadonlyConfig()), missingCloudReadonlyConfig());

  test.beforeEach(async ({ page }) => {
    await page.route("**/api/control/**", async (route) => {
      throw new Error(`cloud_readonly_smoke_must_not_call_control_api:${route.request().url()}`);
    });
  });

  test("账号1只读总览、行情和新闻", async ({ page }) => {
    await login(page, credentials.account1.username, credentials.account1.password);
    await expect(page.getByText(credentials.account1.label).first()).toBeVisible();
    await expect(page.getByText("ETH 趋势策略").first()).toBeVisible();
    await expect(page.getByText("账户与持仓").first()).toBeVisible();
    await expect(page.getByText("AI 决策与仓位").first()).toBeVisible();
    await expect(page.getByText("新闻快讯").first()).toBeVisible();

    await page.getByRole("button", { name: "行情图表" }).click();
    await expect(page.locator("canvas").first()).toBeVisible();

    await switchToMobile(page, credentials.account1.label);
    await page.getByRole("button", { name: "总览" }).click();
    await expect(page.getByText("账户与持仓").first()).toBeVisible();
    await expect(page.getByText("AI 决策与仓位").first()).toBeVisible();
    await expect(page.getByText("新闻快讯").first()).toBeVisible();
    await page.getByRole("button", { name: "图表" }).click();
    await expect(page.getByText("专业行情图表").first()).toBeVisible();
    await expect(page.locator("canvas").first()).toBeVisible();
  });

  test("账号2只读 follower 视图", async ({ page }) => {
    await login(page, credentials.account2.username, credentials.account2.password);
    await expect(page.getByText(credentials.account2.label).first()).toBeVisible();
    await expect(page.getByText("账户与持仓").first()).toBeVisible();
    await expect(page.getByText("AI 决策与仓位").first()).toBeVisible();
    await expect(page.getByText("新闻快讯").first()).toBeVisible();

    await switchToMobile(page, credentials.account2.label);
    await expect(page.getByText("账户与持仓").first()).toBeVisible();
    await expect(page.getByText("AI 决策与仓位").first()).toBeVisible();
    await page.getByRole("button", { name: "快讯" }).click();
    await expect(page.getByText("新闻快讯").first()).toBeVisible();
  });

  test("管理员只读审计页面", async ({ page }) => {
    await login(page, credentials.admin.username, credentials.admin.password);
    await expect(page.getByText(credentials.admin.label).first()).toBeVisible();

    await page.getByRole("button", { name: "交易执行" }).click();
    await expect(page.getByText("最近订单").first()).toBeVisible();
    await expect(page.getByText("持仓闭K复评").first()).toBeVisible();
    await expect(page.getByText("云端发布与回滚审计").first()).toBeVisible();
    await expect(page.getByText("账户 API 与杠杆槽位").first()).toBeVisible();

    await page.getByRole("button", { name: "AI 大脑" }).click();
    await expect(page.getByText("AI 不可越权边界").first()).toBeVisible();
    await expect(page.getByText("最近 AI 决策").first()).toBeVisible();

    await switchToMobile(page, credentials.admin.label);
    await page.getByRole("button", { name: "交易" }).click();
    await expect(page.getByText("最近订单").first()).toBeVisible();
    await expect(page.getByText("持仓闭K复评").first()).toBeVisible();
    await expect(page.getByText("云端发布与回滚审计").first()).toBeVisible();
    await page.getByRole("button", { name: "AI" }).click();
    await expect(page.getByText("AI 不可越权边界").first()).toBeVisible();
    await expect(page.getByText("最近 AI 决策").first()).toBeVisible();
  });
});
