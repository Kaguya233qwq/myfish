import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";

process.env.NO_PROXY = [process.env.NO_PROXY, "127.0.0.1", "localhost"]
  .filter(Boolean)
  .join(",");

const python = fileURLToPath(
  new URL("../../../../../.venv/Scripts/python.exe", import.meta.url),
);
const fixture = fileURLToPath(
  new URL("../../../tests/serve_fishy_ui.py", import.meta.url),
);

export default defineConfig({
  testDir: "./e2e",
  globalTeardown: "./e2e/teardown.ts",
  workers: 1,
  timeout: 40000,
  use: {
    baseURL: "http://127.0.0.1:18787",
    channel: "msedge",
    headless: true,
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `"${python}" "${fixture}"`,
    url: "http://127.0.0.1:18787/api/auth/status",
    timeout: 20000,
    reuseExistingServer: false,
    env: { PYTHONUTF8: "1", NO_PROXY: "127.0.0.1,localhost" },
  },
});
