// Records the demo from the REAL dashboard: healthy system -> one real fault (pause on
// things-search, 12 s maximum, manual mode) -> live impact -> the run's report -> insights.
//
//   node demo/record_demo.mjs            (from the repository root)
//   DEMO_BASE_URL  dashboard, default http://localhost:5173
//   DEMO_API_URL   API,       default http://127.0.0.1:8000
//
// THIS INJECTS A REAL FAULT into the local Ditto sandbox. It refuses to start unless the
// target is healthy, idle and not locked out. Nothing is staged: every number on screen
// and in the subtitles comes from the run it records.
// Output: demo/raw/ditto-chaos-demo-raw.webm, demo/ditto-chaos-demo-subtitles.srt,
//         demo/raw/run.json (the facts of the recorded run).
import { createRequire } from "node:module";
import { mkdirSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const { chromium } = createRequire(join(here, "..", "frontend", "package.json"))("playwright");

const BASE = process.env.DEMO_BASE_URL ?? "http://localhost:5173";
const API = process.env.DEMO_API_URL ?? "http://127.0.0.1:8000";
const RAW = join(here, "raw");
const HDR = { "Content-Type": "application/json", "X-Operator": "demo-operator" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// DEMO_REHEARSE=1 walks every shot WITHOUT injecting: nothing is ticked or run, and the
// report shots use the newest stored experiment. For checking the script, not for a demo.
const REHEARSE = process.env.DEMO_REHEARSE === "1";
const api = async (path) => (await fetch(API + path, { headers: HDR })).json();
const post = (path, body) => fetch(API + path, { method: "POST", headers: HDR, body: body ? JSON.stringify(body) : undefined });

// ---- preconditions (not recorded) ----
const snap = await api("/api/snapshot");
if (snap.health?.status !== "healthy" || !snap.health?.fresh) throw new Error(`target is not healthy (${snap.health?.status}); not recording`);
if (["running", "analyzing"].includes(snap.orchestrator?.status)) throw new Error("an experiment is already in progress");
if (snap.controller?.lockout) throw new Error("recovery lockout is active");
const baselineAge = Date.now() / 1000 - (snap.baseline?.recorded_at ?? 0);
if (baselineAge > (snap.baseline_max_age_s ?? 3600) - 300) throw new Error("baseline is expired or about to expire; record a fresh one first");
const startMode = snap.controller.mode;
await post("/api/orchestrator/reset");                     // clear the previous run's panel
await post("/api/controller/mode", { mode: "recommend" }); // so the video shows the switch to manual

mkdirSync(RAW, { recursive: true });
for (const f of readdirSync(RAW)) if (f.endsWith(".webm")) rmSync(join(RAW, f));
const browser = await chromium.launch();
// A 1920x1080 window at 150% page zoom: full-HD picture, text large enough to read.
const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, colorScheme: "light",
                                           recordVideo: { dir: RAW, size: { width: 1920, height: 1080 } } });
await context.addInitScript(() => {
  const zoom = () => { if (document.documentElement) document.documentElement.style.zoom = "1.5"; };
  zoom();
  document.addEventListener("DOMContentLoaded", zoom);
});
const page = await context.newPage();
const t0 = Date.now();
const now = () => (Date.now() - t0) / 1000;

const subs = [];
function say(text) {
  if (subs.length) subs[subs.length - 1].end = now();
  subs.push({ start: now(), end: null, text });
  console.log(`${now().toFixed(0).padStart(3)}s  ${text}`);
}
const stamp = (s) => {
  const ms = Math.round(s * 1000);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(Math.floor(ms / 3600000))}:${p(Math.floor(ms / 60000) % 60)}:${p(Math.floor(ms / 1000) % 60)},${p(ms % 1000, 3)}`;
};

async function show(locator, offset = 70) {
  await locator.first().evaluate((el, off) => window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - off, behavior: "smooth" }), offset);
  await sleep(1200);
}
async function spot(locator, ms = 1400) { // stands in for the mouse pointer, which a recording does not show
  const el = locator.first();
  await el.evaluate((n) => { n.style.outline = "3px solid #1d4ed8"; n.style.outlineOffset = "3px"; n.style.borderRadius = "8px"; });
  await sleep(ms);
  await el.evaluate((n) => { n.style.outline = ""; n.style.outlineOffset = ""; n.style.borderRadius = ""; });
}
const nav = (name) => page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name });
const panel = (title) => page.locator("section", { has: page.getByRole("heading", { level: 2, name: title, exact: true }) });

const facts = { experiment_id: null };
try {
  // ---- 1. healthy system ----
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { level: 2, name: "Target health" }).waitFor({ timeout: 20000 });
  await page.getByRole("status").getByText("Live", { exact: true }).waitFor({ timeout: 15000 });
  say("ditto-chaos: chaos engineering for an Eclipse Ditto digital-twin sandbox.");
  await sleep(3500);
  say("Before the test, the target is healthy: no errors, no lost updates.");
  await spot(panel("Target health"), 3500);
  say("100 synthetic twins in three product categories, all fresh and valid.");
  await spot(panel("Digital-twin fleet"), 3500);
  say("Nine services are running, and the healthy baseline is valid.");
  await show(panel("Services"), 90);
  await spot(panel("Baseline"), 3000);

  // ---- 2. build and preview the fault ----
  await nav("Experiment control").click();
  await page.getByRole("heading", { level: 1, name: "Experiment control" }).waitFor();
  await page.locator("#fault-0").waitFor({ timeout: 20000 });
  say("The operator switches to manual mode to choose the fault.");
  const manual = page.getByRole("radio", { name: /^manual/i });
  await spot(manual.locator("xpath=.."), 1500);
  await manual.click(); // confirmed by the server, so wait for the header badge
  await page.getByRole("banner").getByText("manual", { exact: true }).first().waitFor({ timeout: 15000 });
  await sleep(1500);

  const builder = panel("Scenario builder");
  await show(builder);
  say("Fault: freeze the search service (pause on things-search), 12 seconds at most.");
  await page.locator("#fault-0").selectOption("pause");
  await spot(page.locator("#fault-0"), 1100);
  await page.locator("#target-0").selectOption("things-search");
  await spot(page.locator("#target-0"), 1100);
  await page.locator("#max-duration").fill("12");
  await spot(page.locator("#max-duration"), 1100);

  say("Preview first: a policy check that executes nothing.");
  await spot(builder.getByRole("button", { name: "Preview plan" }), 1000);
  await builder.getByRole("button", { name: "Preview plan" }).click();
  const previewed = builder.getByText("Preview only. Nothing has been executed.");
  await previewed.waitFor({ timeout: 15000 });
  await show(previewed, 110);
  await sleep(2500);
  say("It lists the exact commands, the rollback order and the stop conditions.");
  await page.evaluate(() => window.scrollBy({ top: 230, behavior: "smooth" }));
  await sleep(4000);

  // ---- 3. authorize and run (REAL) ----
  say("A real fault needs explicit operator authorization.");
  const ack = builder.getByRole("checkbox", { name: /I am an operator and I authorize REAL fault injection/ });
  await show(ack, 430);
  await spot(ack.locator("xpath=.."), 1300);
  const runBtn = builder.getByRole("button", { name: "Run this scenario" });
  if (REHEARSE) {
    await spot(runBtn, 900);
    facts.experiment_id = (await api("/api/experiments?limit=40")).find((e) => e.injected && !e.simulated)?.id ?? null; // newest REAL run
    await show(page.getByRole("heading", { level: 1, name: "Experiment control" }));
  } else {
    await ack.check();
    await sleep(900);
    await spot(runBtn, 900);
    await runBtn.click();
    facts.run_clicked_at_s = now();
    await page.getByRole("heading", { level: 2, name: "Running experiment" }).waitFor({ timeout: 20000 });
    await show(panel("Running experiment"));
  }
  say("The controller checks policy, containers and current health before touching anything.");
  await sleep(3500);

  // ---- 4. live impact: the Pipeline tab first, then Live monitoring ----
  // Both are reached by a click in the sidebar while the run continues, so the fault
  // interval is recorded in real time with nothing cut out of it.
  await nav("Pipeline").click();
  await page.getByRole("heading", { level: 1, name: "Pipeline" }).waitFor();
  say("The Pipeline tab follows the run stage by stage, from the controller's own events.");
  await show(panel("Before the fault"), 60);
  let onCharts = false;
  const toCharts = async () => {
    onCharts = true;
    await nav("Live monitoring").click();
    await page.getByRole("heading", { level: 2, name: "Request failures" }).waitFor();
    await page.getByRole("button", { name: "5 min", exact: true }).click();
    await show(panel("Request latency"), 90);
  };
  const lines = {
    BASELINE: "Stage 4, Baseline: health is confirmed before anything is touched.",
    MONITORING: "Fault injected: the search service is frozen. The Monitor stage counts failed checks toward the kill switch.",
    REVERTING: "The run is being stopped and the fault rolled back.",
    VERIFYING_RECOVERY: "Rollback verified. Measuring how long Ditto takes to recover.",
  };
  let last = "", status = "", monitoringSince = null;
  const deadline = Date.now() + 200000;
  if (REHEARSE) await sleep(4000);
  while (!REHEARSE && Date.now() < deadline) {
    const st = await api("/api/orchestrator/status");
    facts.experiment_id = st.record?.id ?? facts.experiment_id;
    const state = st.record?.state;
    if (state !== last) {
      facts[`state_${state}_at_s`] = now();
      if (state === "REVERTING") {
        const why = st.record?.abort_reason;
        say(why ? `Kill switch: ${why}. Rolling back automatically.` : lines.REVERTING);
      } else if (lines[state]) say(lines[state]);
      if (state === "MONITORING") {
        monitoringSince = Date.now();
        if (!onCharts) await show(panel("During and after"), 90);
      }
    }
    last = state;
    if (st.status !== status && st.status === "analyzing") say("Recovered. The agents now analyse the measurements.");
    status = st.status;
    if (st.status === "done" || st.status === "error") break;
    // After a few seconds of the fault on the Pipeline tab, watch the rest of it on the charts.
    if (!onCharts && monitoringSince !== null && Date.now() - monitoringSince > 6500) {
      await toCharts();
      say("Live monitoring shows the same fault on the charts, as it happens.");
    }
    await sleep(500);
  }
  if (!onCharts) await toCharts();
  const rec = REHEARSE ? ((await api(`/api/experiments/${facts.experiment_id}`)).record ?? (await api(`/api/experiments/${facts.experiment_id}`))) : ((await api("/api/orchestrator/status")).record ?? {});
  Object.assign(facts, { state: rec.state, stop_reason: rec.abort_reason ?? null, detected_s: rec.detected_s ?? null,
                         recovery_outcome: rec.recovery_outcome, recovery_s: rec.recovery_s, rollback_verified: rec.rollback_verified,
                         actions: (rec.actions ?? []).map((a) => ({ fault: a.fault, target: a.target, status: a.status, rollback_verified: a.rollback_verified })) });
  await sleep(5000); // let the probe's newest rows reach the charts
  say("The charts show the impact: search failures during the fault, then back to normal.");
  await spot(panel("Request failures"), 4500);
  await sleep(1000);

  // ---- 4b. the finished pipeline ----
  await nav("Pipeline").click();
  await page.getByRole("heading", { level: 1, name: "Pipeline" }).waitFor();
  say(`Back on the Pipeline: ${rec.abort_reason ? "Monitor was stopped by the kill switch" : "every stage is done"}, and the rollback is ${rec.rollback_verified ? "verified" : "NOT verified"}.`);
  await show(panel("During and after"), 90);
  await sleep(6000);
  say("The agents lane shows what each of the seven agents concluded for this run.");
  await show(panel("Agents"), 90);
  await sleep(5500);

  // ---- 5. the report ----
  await nav("History").click();
  await page.getByRole("heading", { level: 1, name: "Experiment history" }).waitFor();
  const link = page.locator(`a[href="/history/${facts.experiment_id}"]`).first();
  await spot(link, 1300);
  await link.click();
  await page.getByRole("heading", { level: 2, name: "What happened, step by step" }).waitFor({ timeout: 20000 });
  const rb = rec.rollback_verified ? "rollback verified" : "ROLLBACK NOT VERIFIED";
  const recov = rec.recovery_outcome === "recovered" ? `recovered in ${Number(rec.recovery_s).toFixed(1)} s` : `recovery: ${rec.recovery_outcome}`;
  say(`The report: ${rec.abort_reason ? "stopped by the kill switch" : "ran its full time"}, ${rb}, ${recov}.`);
  await spot(panel("Run"), 6000);
  say("Impact is reported per product category, from measured data only.");
  await show(panel("Impact by product category"));
  await sleep(4500);
  const findings = panel("Findings");
  const firstFinding = (await findings.locator("h3, h4, .font-medium").first().textContent().catch(() => null))?.trim() ?? null;
  facts.first_finding_text = firstFinding;
  say("Findings: measured facts are labelled separately from hypotheses.");
  await show(findings);
  await sleep(6500);
  const recs = panel("Recommendations");
  facts.first_recommendation_text = (await recs.locator("h3, h4, .font-medium").first().textContent().catch(() => null))?.trim() ?? null;
  say("Recommended fixes are proposed for an operator to decide. Nothing is applied automatically.");
  await show(recs);
  await sleep(7000);

  // ---- 6. insights ----
  await nav("Insights & learning").click();
  await page.getByRole("heading", { level: 2, name: "Evidence base" }).waitFor({ timeout: 20000 });
  say("Every run adds to the evidence base and re-ranks which experiment to run next.");
  await spot(panel("Evidence base"), 3500);
  await show(panel("Next-experiment ranking"), 90);
  await sleep(4500);
  say("Injected, observed, stopped, verified, explained.");
  await show(panel("Latest analysis"), 90);
  await sleep(5000);
  subs[subs.length - 1].end = now();
  facts.length_s = now();
  console.log(`${now().toFixed(0).padStart(3)}s  end`);
} finally {
  await context.close(); // flushes the video file
  await browser.close();
  await post("/api/controller/mode", { mode: startMode });
  const made = readdirSync(RAW).filter((f) => f.endsWith(".webm"));
  if (made.length) renameSync(join(RAW, made[0]), join(RAW, "ditto-chaos-demo-raw.webm"));
  if (subs.length && subs[subs.length - 1].end === null) subs[subs.length - 1].end = now();
  writeFileSync(join(here, "ditto-chaos-demo-subtitles.srt"),
    subs.map((s, i) => `${i + 1}\n${stamp(s.start)} --> ${stamp(Math.max(s.end - 0.1, s.start + 1))}\n${s.text}\n`).join("\n"), "utf8");
  writeFileSync(join(RAW, "run.json"), JSON.stringify(facts, null, 2), "utf8");
  console.log("\n" + JSON.stringify(facts, null, 2));
}
