import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const { chromium } = require("/usr/local/lib/node_modules/@playwright/cli/node_modules/playwright");
const fixture = Bun.spawn([process.execPath, "serve.ts"], { stdout: "ignore", stderr: "pipe" });
let browser: any;
try {
  let ready = false;
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    assert.equal(fixture.exitCode, null, "Frontend server exited before readiness");
    try {
      const response = await fetch("http://127.0.0.1:8000", { signal: AbortSignal.timeout(500) });
      ready = response.ok && (await response.text()).includes("<title>Dev container pilot</title>");
    } catch {}
    if (ready) break;
    await Bun.sleep(100);
  }
  assert(ready, "Frontend server was not ready within five seconds");
  browser = await chromium.launch({ channel: "chromium", headless: true });
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 }, deviceScaleFactor: 1, locale: "en-US", timezoneId: "UTC", colorScheme: "light" });
  await page.goto("http://127.0.0.1:8000");
  await page.getByRole("textbox", { name: "Name" }).fill("Ada");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "Saved Ada" }).waitFor();
  const text = await page.getByRole("status").textContent();
  assert.equal(text, "Saved Ada");
  await page.evaluate(() => document.fonts.ready);
  const directory = join(process.env.HOME!, ".local/share/dev-ui/artifacts");
  mkdirSync(directory, { recursive: true });
  const screenshot = join(directory, "frontend.png");
  await page.screenshot({ path: screenshot, animations: "disabled", caret: "hide" });
  assert.equal(fixture.exitCode, null, "Frontend server exited during verification");
  const screenshotSha256 = createHash("sha256").update(readFileSync(screenshot)).digest("hex");
  console.log(JSON.stringify({ text, screenshotSha256 }));
} finally {
  try { if (browser) await browser.close(); }
  finally { fixture.kill(); await fixture.exited; }
}
