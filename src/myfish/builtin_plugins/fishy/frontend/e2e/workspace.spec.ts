import { expect, test } from "@playwright/test";
import type { Page, Request } from "@playwright/test";
import type { Product } from "../src/types";

const product: Product = {
  id: 42,
  title: "测试资料",
  description: "说明",
  price: "9.90",
  account_id: "11",
  item_id: "99",
  mode: "reusable",
  images: "[]",
  category: "{}",
  product_type: "virtual",
  quantity: 1,
  shipping: "free",
  postage: "0",
  auto_delivery: 0,
  status: "published",
  auto_polish: 1,
  polish_interval_hours: 24,
  polish_next_at: 0,
  polish_last_at: 0,
  polish_status: "waiting",
  polish_error: "",
  draft_id: "",
  draft_state: "local",
  draft_synced_at: 0,
  draft_error: null,
  edit_state: "",
};

async function workspace(page: Page, role = "admin") {
  const requests: Request[] = [];
  const errors: string[] = [];
  let authenticated = true;
  let jobStatus = "pending";
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    requests.push(request);
    const path = new URL(request.url()).pathname.slice(4);
    let body: unknown = {};
    if (path === "/auth/status") body = { needs_setup: false };
    else if (path === "/auth/me")
      body = {
        id: 1,
        username: "tester",
        role,
        csrf: "test-csrf",
        session_id: 7,
      };
    else if (path === "/products")
      body = [
        product,
        {
          ...product,
          id: 43,
          title: "下载链接",
          item_id: "100",
          mode: "reusable",
        },
      ];
    else if (path.endsWith("/delivery-content"))
      body = { content: "https://old.example/download" };
    else if (path === "/orders") body = [];
    else if (path === "/jobs")
      body = [
        {
          id: 3,
          kind: "polish",
          target: "42",
          status: jobStatus,
          run_at: 1900000000,
          error: "",
        },
      ];
    else if (path === "/jobs/3/cancel") jobStatus = "cancelled";
    else if (path === "/accounts") body = [{ id: "11", connected: true }];
    else if (path === "/dashboard")
      body = { products: 2, pending_jobs: 1, orders: 0, attention: 0 };
    else if (path === "/auth/sessions" && request.method() === "GET")
      body = [{ id: 7, created: 1, expires: 2000000000 }];
    else if (path === "/auth/sessions/7") authenticated = false;
    else if (path === "/users")
      body = [{ id: 1, username: "tester", role, active: 1 }];
    else if (path === "/audit")
      body = [
        {
          id: 1,
          actor: "tester",
          action: "product:create",
          target: "42",
          created: 1,
        },
      ];
    if (!authenticated && request.method() === "GET") {
      await route.fulfill({ status: 401, json: { detail: "登录已失效" } });
    } else await route.fulfill({ json: body });
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "工作台", exact: true }),
  ).toBeVisible();
  return { requests, errors };
}

test("product search and dialog keyboard navigation survive page extraction", async ({
  page,
}) => {
  const { errors } = await workspace(page);
  await page.getByRole("button", { name: "商品管理", exact: true }).click();
  const search = page.getByPlaceholder("搜索商品名称或平台 ID");
  await search.fill("100");
  await page.getByRole("button", { name: "交易订单", exact: true }).click();
  await page.getByRole("button", { name: "商品管理", exact: true }).click();
  await expect(search).toHaveValue("100");
  await expect(
    page.getByRole("row").filter({ hasText: "测试资料" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("row").filter({ hasText: "下载链接" }),
  ).toBeVisible();
  await search.fill("missing");
  await expect(
    page.getByRole("heading", { name: "没有匹配的商品" }),
  ).toBeVisible();
  await search.clear();
  const trigger = page
    .getByRole("button", { name: "编辑交付内容", exact: true })
    .first();
  await trigger.click();
  const close = page.getByRole("button", { name: "关闭弹窗" });
  await expect(close).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(
    page.getByRole("button", { name: "保存交付内容", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(close).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(trigger).toBeFocused();
  expect(errors).toEqual([]);
});

test("failed form retains input, forwards CSRF, and successful retry closes it", async ({
  page,
}) => {
  const { requests, errors } = await workspace(page);
  let reject = true;
  await page.route("**/api/products/42/delivery-content", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        json: { content: "https://old.example/download" },
      });
      return;
    }
    requests.push(route.request());
    await route.fulfill(
      reject ? { status: 400, json: { detail: "模拟保存失败" } } : { json: {} },
    );
  });
  await page.getByRole("button", { name: "商品管理", exact: true }).click();
  await page
    .getByRole("button", { name: "编辑交付内容", exact: true })
    .first()
    .click();
  await page.getByLabel("固定交付内容").fill("https://example.com/download");
  await page.getByRole("button", { name: "保存交付内容", exact: true }).click();
  await expect(
    page.getByRole("dialog").getByText("模拟保存失败", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("固定交付内容")).toHaveValue(
    "https://example.com/download",
  );
  expect(requests.at(-1)?.headers()["x-csrf-token"]).toBe("test-csrf");
  expect(requests.at(-1)?.postDataJSON()).toEqual({
    content: "https://example.com/download",
  });
  reject = false;
  await page.getByRole("button", { name: "保存交付内容", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("status")).toContainText("交付内容已更新");
  expect(errors).toEqual([]);
});

test("scheduled listing action and task cancellation use their original endpoints", async ({
  page,
}) => {
  const { requests, errors } = await workspace(page);
  await page.getByRole("button", { name: "商品管理", exact: true }).click();
  await page
    .getByRole("row")
    .filter({ hasText: "测试资料" })
    .getByRole("button", { name: "下架", exact: true })
    .click();
  await page.getByLabel("执行时间").fill("2030-01-02T12:30");
  await page.getByRole("button", { name: "确认下架商品", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const scheduled = requests.find((request) =>
    request.url().endsWith("/products/42/delist"),
  );
  expect(scheduled?.method()).toBe("POST");
  expect(scheduled?.postDataJSON()).toEqual({
    run_at: new Date("2030-01-02T12:30").getTime() / 1000,
  });
  await page.getByRole("button", { name: /^任务队列/ }).click();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "已取消", exact: true }),
  ).toBeVisible();
  expect(
    requests.some(
      (request) =>
        request.url().endsWith("/jobs/3/cancel") && request.method() === "POST",
    ),
  ).toBeTruthy();
  expect(errors).toEqual([]);
});

test("a pending form cannot close or submit twice", async ({ page }) => {
  const { errors } = await workspace(page);
  let release!: () => void;
  const responseReady = new Promise<void>((resolve) => {
    release = resolve;
  });
  let submissions = 0;
  await page.route("**/api/products/42/delivery-content", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        json: { content: "https://old.example/download" },
      });
      return;
    }
    submissions += 1;
    await responseReady;
    await route.fulfill({ json: {} });
  });
  await page.getByRole("button", { name: "商品管理", exact: true }).click();
  await page
    .getByRole("button", { name: "编辑交付内容", exact: true })
    .first()
    .click();
  await page.getByLabel("固定交付内容").fill("https://example.com/download");
  await page.getByRole("button", { name: "保存交付内容", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "保存交付内容", exact: true }),
  ).toBeDisabled();
  await expect(page.getByRole("button", { name: "关闭弹窗" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeVisible();
  release();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(submissions).toBe(1);
  expect(errors).toEqual([]);
});

test("yellow theme and reduced-motion preference remain available", async ({
  page,
}) => {
  const { errors } = await workspace(page);
  await expect(
    page.getByRole("button", { name: "新建商品", exact: true }),
  ).toHaveCSS("background-color", "rgb(255, 230, 15)");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.getByRole("button", { name: "新建商品", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  const durations = await page.getByRole("dialog").evaluate((element) => ({
    animation: getComputedStyle(element).animationDuration,
    transition: getComputedStyle(element).transitionDuration,
  }));
  expect(parseFloat(durations.animation)).toBeLessThanOrEqual(0.001);
  expect(parseFloat(durations.transition)).toBeLessThanOrEqual(0.001);
  await page.keyboard.press("Escape");
  expect(errors).toEqual([]);
});

test("operator navigation and revoking the current session", async ({
  page,
}) => {
  const { requests, errors } = await workspace(page, "operator");
  await expect(
    page.getByRole("button", { name: "团队账号", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "操作记录", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "账号安全", exact: true }).click();
  await expect(
    page.getByText("会话 #7 · 当前会话", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  expect(
    requests.some(
      (request) =>
        request.url().endsWith("/auth/sessions/7") &&
        request.method() === "DELETE",
    ),
  ).toBeTruthy();
  expect(errors).toEqual([]);
});

test("polish feedback is concise and retains cooldown times and uncertain results", async ({
  page,
}) => {
  const { errors } = await workspace(page);
  await page.route("**/api/products", (route) =>
    route.fulfill({
      json: [
        { ...product, polish_status: "already_polished" },
        {
          ...product,
          id: 43,
          title: "待核对商品",
          status: "status_unknown",
          polish_status: "uncertain",
          polish_error: "擦亮结果待核对，请检查闲鱼商品状态",
        },
      ],
    }),
  );
  await page.route("**/api/jobs", (route) =>
    route.fulfill({
      json: [
        {
          id: 3,
          kind: "polish",
          target: "42",
          status: "skipped",
          run_at: 1900000000,
          error: "平台确认该商品已擦亮，本次未新增擦亮",
        },
      ],
    }),
  );
  await page.route("**/api/products/42/polish", (route) =>
    route.fulfill({
      status: 400,
      json: {
        detail:
          "平台已确认该商品已擦亮，下次可擦亮时间：2030-01-02T12:30:00+08:00",
      },
    }),
  );
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await page.getByRole("button", { name: "商品管理", exact: true }).click();
  const row = page.getByRole("row").filter({ hasText: "测试资料" });
  await expect(row.getByText("已擦亮", { exact: true })).toBeVisible();
  const uncertain = page.getByRole("row").filter({ hasText: "待核对商品" });
  await expect(
    uncertain.getByText("状态待确认", { exact: true }),
  ).toBeVisible();
  await expect(
    uncertain.getByText("擦亮结果待核对，请检查闲鱼商品状态", { exact: true }),
  ).toBeVisible();
  await expect(uncertain.getByText("已擦亮", { exact: true })).toHaveCount(0);
  await row.getByRole("button", { name: "立即擦亮", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "已擦亮，下次擦亮时间：2030-01-02T12:30:00+08:00×",
  );
  await page.getByRole("button", { name: /^任务队列/ }).click();
  const job = page.getByRole("row").filter({ hasText: "#3" });
  await expect(job.getByText("已跳过", { exact: true })).toBeVisible();
  await expect(job.getByText("已擦亮", { exact: true })).toBeVisible();
  await expect(page.getByText(/平台确认|平台已确认/)).toHaveCount(0);
  expect(errors).toEqual([]);
});
