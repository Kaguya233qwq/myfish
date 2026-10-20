import { test, expect } from "@playwright/test";

test("initialize, manage products, deliver fixed content, and revoke login", async ({
  page,
}) => {
  const errors: string[] = [];
  await page.route("https://img.alicdn.com/**", (route) =>
    route.fulfill({
      contentType: "image/png",
      body: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS9sAAAAASUVORK5CYII=",
        "base64",
      ),
    }),
  );
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "创建第一个管理员" }),
  ).toBeVisible();
  await page
    .getByLabel("初始化令牌")
    .fill("playwright-test-only-setup-token-123456");
  await page.getByLabel("账号", { exact: true }).fill("admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("browser-test-strong-password-123");
  await page.getByRole("button", { name: "初始化并登录" }).click();
  await expect(
    page.getByRole("heading", { name: "工作台", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "新建商品", exact: true }).click();
  await page.getByLabel("商品名称").fill("UI 测试资料");
  await page
    .getByLabel("交付内容", { exact: true })
    .fill("https://example.com/download");
  await expect(page.getByText("独立卡密库存", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("商品品类")).toBeEnabled();
  await page.getByLabel("商品品类").selectOption("202036301");
  await page.getByLabel("已有闲鱼商品 ID（可选）").fill("99");
  await page.getByRole("button", { name: "保存商品档案", exact: true }).click();
  await page.getByRole("button", { name: "商品管理", exact: true }).click();
  await expect(page.getByText("UI 测试资料", { exact: true })).toBeVisible();
  let digitalRow = page.getByRole("row").filter({ hasText: "UI 测试资料" });
  await digitalRow
    .getByRole("button", { name: "设置自动擦亮", exact: true })
    .click();
  await page.getByLabel("开启自动擦亮", { exact: true }).check();
  await page.getByLabel("擦亮间隔（小时）", { exact: true }).fill("48");
  await page.screenshot({
    path: "test-results/automatic-polish.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "保存擦亮设置", exact: true }).click();
  await expect(async () => {
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await expect(digitalRow.getByText("已擦亮", { exact: true })).toBeVisible();
    await expect(
      digitalRow.getByRole("button", { name: "立即擦亮", exact: true }),
    ).toBeDisabled();
  }).toPass({ timeout: 15000 });
  await digitalRow
    .getByRole("button", { name: "自动擦亮已开启", exact: true })
    .click();
  await page.getByLabel("开启自动擦亮", { exact: true }).uncheck();
  await page.getByRole("button", { name: "保存擦亮设置", exact: true }).click();
  await expect(
    digitalRow.getByRole("button", { name: "设置自动擦亮", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "新建商品", exact: true }).click();
  await page.getByLabel("商品类型").selectOption("physical");
  await page.getByLabel("商品名称").fill("UI 测试实物T恤");
  await expect(page.getByLabel("商品品类")).toBeEnabled();
  await expect(page.getByLabel("商品品类")).toHaveValue("");
  await page.getByLabel("商品品类").selectOption("126910002");
  await page.getByLabel("发布数量").fill("3");
  await page.getByLabel("配送运费").selectOption("fixed");
  await page.getByLabel("运费（元）", { exact: true }).fill("8.50");
  await expect(page.getByLabel("开启自动交付")).toHaveCount(0);
  await page
    .getByRole("button", { name: "添加并同步草稿", exact: true })
    .click();
  await expect(page.getByText("UI 测试实物T恤", { exact: true })).toBeVisible();
  await expect(async () => {
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await expect(
      page.getByText("已同步闲鱼草稿箱", { exact: true }),
    ).toBeVisible();
  }).toPass({ timeout: 15000 });
  await page
    .getByRole("row")
    .filter({ hasText: "UI 测试实物T恤" })
    .getByRole("button", { name: "编辑", exact: true })
    .click();
  await expect(page.getByLabel("商品品类")).toHaveValue("126910002");
  await expect(page.getByLabel("发布数量")).toHaveValue("3");
  await page.getByLabel("售价（元）").fill("19.90");
  await page
    .getByRole("button", { name: "保存并同步草稿", exact: true })
    .click();
  await expect(async () => {
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await expect(
      page.getByText("已同步闲鱼草稿箱", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("草稿 ID · 54321", { exact: true }),
    ).toBeVisible();
  }).toPass({ timeout: 15000 });
  await page
    .getByRole("row")
    .filter({ hasText: "UI 测试实物T恤" })
    .getByRole("button", { name: "编辑", exact: true })
    .click();
  expect(Number(await page.getByLabel("售价（元）").inputValue())).toBe(19.9);
  await page.getByLabel("商品类型").selectOption("virtual");
  await expect(page.getByLabel("商品品类")).toHaveValue("");
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByRole("button", { name: "补库存" })).toHaveCount(0);
  await digitalRow
    .getByRole("button", { name: "编辑并发布", exact: true })
    .click();
  await expect(page.getByLabel("商品标题", { exact: true })).toHaveValue(
    "UI 测试资料",
  );
  await expect(page.getByText("编辑草稿", { exact: true })).toBeVisible();
  await expect(page.getByText(/候选|保持不变/)).toHaveCount(0);
  await page.getByLabel("商品标题", { exact: true }).fill("UI 更新资料");
  await page.getByLabel("商品描述", { exact: true }).fill("后台编辑说明");
  await page.getByLabel("商品售价（元）", { exact: true }).fill("19.99");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByText("草稿已保存", { exact: true })).toBeVisible();
  await expect(digitalRow).toBeVisible();
  await page
    .getByRole("button", { name: "提交编辑并发布", exact: true })
    .click();
  await expect(
    page.getByText("编辑任务已加入队列", { exact: true }),
  ).toBeVisible();
  await page.locator(".modal-heading .icon-button").click();
  digitalRow = page.getByRole("row").filter({ hasText: "UI 更新资料" });
  await expect(async () => {
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await expect(
      digitalRow.getByText("闲鱼 ID · 99", { exact: true }),
    ).toBeVisible();
    await expect(digitalRow.getByText("19.99", { exact: false })).toBeVisible();
  }).toPass({ timeout: 15000 });
  await digitalRow.getByRole("button", { name: "下架", exact: true }).click();
  await page.getByRole("button", { name: "确认下架商品", exact: true }).click();
  await expect(async () => {
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await expect(
      digitalRow.getByRole("button", { name: "恢复上架", exact: true }),
    ).toBeVisible();
  }).toPass({ timeout: 15000 });
  await digitalRow
    .getByRole("button", { name: "同步状态", exact: true })
    .click();
  await digitalRow
    .getByRole("button", { name: "恢复上架", exact: true })
    .click();
  await page.getByRole("button", { name: "确认恢复上架", exact: true }).click();
  await expect(async () => {
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await expect(digitalRow.getByText("在售", { exact: true })).toBeVisible();
    await expect(
      digitalRow.getByRole("button", { name: "恢复上架", exact: true }),
    ).toHaveCount(0);
    await expect(
      digitalRow.getByText("闲鱼 ID · 99", { exact: true }),
    ).toBeVisible();
  }).toPass({ timeout: 15000 });
  await page.getByRole("button", { name: "交易订单", exact: true }).click();
  await page.getByRole("button", { name: "同步 11", exact: true }).click();
  await expect(async () => {
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "关联会话", exact: true }),
    ).toBeVisible();
  }).toPass({ timeout: 15000 });
  await page.getByRole("button", { name: "关联会话", exact: true }).click();
  await expect(page.getByText("¥9.90", { exact: true })).toBeVisible();
  await expect(page.getByText("UI测试买家", { exact: true })).toBeVisible();
  await expect(page.getByText("ID: 22", { exact: true })).toBeVisible();
  await page.getByLabel("会话 ID").fill("test-conversation");
  await page.getByRole("button", { name: "确认关联" }).click();
  await page.getByRole("button", { name: "请求交付", exact: true }).click();
  await expect(async () => {
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await expect(page.getByText("已完成", { exact: true })).toBeVisible();
  }).toPass({ timeout: 15000 });
  await page.getByRole("button", { name: "商品管理", exact: true }).click();
  await page.getByRole("button", { name: "新建商品", exact: true }).click();
  await page.getByLabel("商品名称").fill("UI 固定链接商品");
  await page.getByLabel("商品品类").selectOption("202036301");
  await page.getByLabel("已有闲鱼商品 ID（可选）").fill("1001");
  await page
    .getByLabel("交付内容", { exact: true })
    .fill("https://old.example/file");
  await page.getByRole("button", { name: "保存商品档案", exact: true }).click();
  const reusableRow = page
    .getByRole("row")
    .filter({ hasText: "UI 固定链接商品" });
  await reusableRow
    .getByRole("button", { name: "编辑交付内容", exact: true })
    .click();
  await expect(page.getByLabel("固定交付内容", { exact: true })).toHaveValue(
    "https://old.example/file",
  );
  await page
    .getByLabel("固定交付内容", { exact: true })
    .fill("https://new.example/file");
  const contentUpdate = page.waitForResponse(
    (response) =>
      response.url().endsWith("/delivery-content") &&
      response.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "保存交付内容", exact: true }).click();
  expect((await contentUpdate).ok()).toBeTruthy();
  await reusableRow
    .getByRole("button", { name: "编辑交付内容", exact: true })
    .click();
  await expect(page.getByLabel("固定交付内容", { exact: true })).toHaveValue(
    "https://new.example/file",
  );
  await page.locator(".modal-heading .icon-button").click();
  await page.getByRole("button", { name: "团队账号", exact: true }).click();
  await page.getByRole("button", { name: /^任务队列/ }).click();
  await expect(
    page.getByText("结果待核对的远端操作不会盲目重试", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "核对结果", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("row")
    .filter({ hasText: "log-test" })
    .getByRole("button", { name: "查看错误日志", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "任务错误日志", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("FishAPIError: 模拟任务失败", { exact: false }),
  ).toBeVisible();
  await page.locator(".modal-heading .icon-button").click();
  await page.getByRole("button", { name: "团队账号", exact: true }).click();
  await page.getByRole("button", { name: "添加成员", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByLabel("账号", { exact: true })
    .fill("operator");
  await page.getByLabel("初始密码").fill("operator-strong-password-123");
  await page.getByRole("button", { name: "创建账号" }).click();
  await expect(page.getByText("operator", { exact: true })).toBeVisible();
  await page.screenshot({
    path: "test-results/management-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "工作台", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "工作台", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/management-mobile.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  expect(errors).toEqual([]);
});
