// Visits every core page of a running instance and saves full-page screenshots plus a report
// (HTTP status, heading, console errors, horizontal overflow) to artifacts/screenshots/.
// Usage (from frontend/): UI_URL=http://127.0.0.1:3000 API_URL=http://127.0.0.1:8000 node scripts/screenshots.mjs
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const UI = process.env.UI_URL ?? "http://127.0.0.1:3000";
const API = process.env.API_URL ?? "http://127.0.0.1:8000";
const OUT = fileURLToPath(new URL("../../artifacts/screenshots/", import.meta.url));
mkdirSync(OUT, { recursive: true });

const events = await (await fetch(`${API}/api/events?limit=50`)).json();
const event = events.find((e) => e.verification_status === "CONFIRMED") ?? events[0];
const pages = [
  ["01-dashboard", "/"],
  ["02-asset-BTC", "/assets/BTC"],
  ["03-asset-EURUSD", "/assets/EURUSD"],
  ["04-events", "/events"],
  ...(event ? [["05-event-detail", `/events/${event.id}`]] : []),
  ["06-calendar", "/calendar"],
  ["07-fx", "/fx"],
  ["08-sources", "/sources"],
  ["09-providers", "/providers"],
  ["10-alerts", "/alerts"],
  ["11-social", "/social"],
  ["12-performance", "/performance"],
  ["13-settings", "/settings"],
];
const MOBILE = new Set(["01-dashboard", "02-asset-BTC", "05-event-detail"]);

const browser = await chromium.launch();
const report = [];
for (const [viewport, suffix] of [
  [{ width: 1440, height: 900 }, ""],
  [{ width: 390, height: 844 }, "-mobile"],
]) {
  const context = await browser.newContext({ viewport, colorScheme: "dark" });
  for (const [name, path] of pages) {
    if (suffix && !MOBILE.has(name)) continue;
    const page = await context.newPage();
    const errors = [];
    page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
    page.on("pageerror", (e) => errors.push(String(e)));
    const response = await page
      .goto(UI + path, { waitUntil: "networkidle", timeout: 60000 })
      .catch((e) => ({ status: () => String(e) }));
    await page.waitForTimeout(2500); // charts and queries settle
    const heading = await page.locator("h1").first().textContent().catch(() => null);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    await page.screenshot({ path: `${OUT}${name}${suffix}.png`, fullPage: true });
    report.push({ page: name + suffix, path, status: response.status(), heading, errors, horizontal_overflow: overflow });
    await page.close();
  }
  await context.close();
}
await browser.close();
writeFileSync(`${OUT}report.json`, JSON.stringify(report, null, 2));
for (const r of report)
  console.log(r.status, r.page, JSON.stringify(r.heading), r.errors.length ? r.errors.slice(0, 3) : "", r.horizontal_overflow ? "OVERFLOW" : "");
