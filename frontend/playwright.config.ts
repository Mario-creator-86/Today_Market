import { defineConfig } from "@playwright/test";

// PLAYWRIGHT_BASE_URL runs the suite against an already running build (e.g. `next start`) instead of a dev server.
const external = process.env.PLAYWRIGHT_BASE_URL;

export default defineConfig({
  testDir: "./tests",
  use: { baseURL: external ?? "http://127.0.0.1:3000", headless: true },
  webServer: external
    ? undefined
    : {
        command: "npm run dev",
        url: "http://127.0.0.1:3000",
        reuseExistingServer: !process.env.CI,
        timeout: 120000,
      },
});
