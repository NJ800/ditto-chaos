// Captures every page at desktop and 375px mobile widths into frontend/screenshots/.
// Usage: node e2e/shots.mjs   (E2E_BASE_URL defaults to the dev server on :5173)
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:5173";
const PAGES = [["overview", "/"], ["monitoring", "/monitoring"], ["fleet", "/fleet"], ["control", "/control"],
               ["agents", "/agents"], ["history", "/history"], ["insights", "/insights"], ["dependencies", "/dependencies"]];
const SIZES = [["desktop", 1440, 900], ["mobile", 375, 812]];

mkdirSync("screenshots", { recursive: true });
const browser = await chromium.launch();
let problems = 0;
for (const [size, width, height] of SIZES) {
  const page = await browser.newPage({ viewport: { width, height } });
  page.on("console", (m) => { if (m.type() === "error") { problems++; console.log(`[console.error ${size}] ${m.text()}`); } });
  page.on("pageerror", (e) => { problems++; console.log(`[pageerror ${size}] ${e.message}`); });
  for (const [name, path] of PAGES) {
    await page.goto(BASE + path, { waitUntil: "domcontentloaded" });
    await page.locator("main h1").first().waitFor({ timeout: 20000 });
    await page.waitForTimeout(1200);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    if (overflow > 1) console.log(`[overflow ${size}] ${name}: page is ${overflow}px wider than the viewport`);
    await page.screenshot({ path: `screenshots/${name}-${size}.png`, fullPage: true });
    console.log(`saved screenshots/${name}-${size}.png`);
  }
  await page.close();
}
await browser.close();
console.log(problems ? `${problems} console/page error(s)` : "no console or page errors");
process.exit(problems ? 1 : 0);
