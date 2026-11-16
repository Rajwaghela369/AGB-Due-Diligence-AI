// End to end against the real API with fake model clients (app/api/testing.py), so the run
// needs no keys, no network and spends no Gemini calls. Ports 8010 and 5180 stay clear of the
// development stack (8000, 3000, 5173).
import { defineConfig, devices } from "@playwright/test";

const API_PORT = 8010;
const WEB_PORT = 5180;

export default defineConfig({
  testDir: "./e2e",
  // One server holds the review the tests share, so they run in order in one worker.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    locale: "de-DE",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `uv run uvicorn app.api.testing:build_test_app --factory --host 127.0.0.1 --port ${API_PORT}`,
      cwd: "..",
      url: `http://127.0.0.1:${API_PORT}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `pnpm exec vite --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      env: { API_PROXY: `http://127.0.0.1:${API_PORT}` },
      url: `http://127.0.0.1:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
