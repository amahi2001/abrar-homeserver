// Run inside the built image via ui-smoke.sh; no model calls or real credentials.
import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";

const artifacts = join(process.env.HOME!, ".local/share/dev-ui/artifacts");
function command(args: string[], ok = true) {
  const r = Bun.spawnSync(args, { stdout: "pipe", stderr: "pipe" });
  const out = r.stdout.toString() + r.stderr.toString();
  if (ok) assert.equal(r.exitCode, 0, `${args.join(" ")}\n${out}`);
  return { code: r.exitCode, out };
}
const cli = (...args: string[]) => command(["playwright-cli", ...args]).out;
const ui = (...args: string[]) => command(["dev-ui", ...args]).out;
async function until(check: () => Promise<boolean> | boolean, message: string) {
  for (let i = 0; i < 100; i++) { if (await check()) return; await Bun.sleep(100); }
  throw Error(message);
}

const fixedFlag = "/tmp/dev-ui-smoke-fixed";
if (process.argv[2] === "serve") {
Bun.serve({ hostname: "127.0.0.1", port: 8000, fetch(request) {
  const fixed = existsSync(fixedFlag);
  if (new URL(request.url).pathname === "/broken") return new Response("fixture failure", { status: 500 });
  return new Response(`<!doctype html><title>UI smoke</title><style>body{margin:32px;font:24px sans-serif}button{background:${fixed ? "lightgreen" : "salmon"};padding:12px}</style>
    <label>Name<input id="name" aria-label="Name"></label><button id="save">Save</button><p role="status">Ready</p>
    <script>save.onclick=()=>{document.querySelector('[role=status]').textContent='Saved '+nameInput.value;${fixed ? "" : "console.error('fixture console failure');fetch('/broken');"}};const nameInput=document.getElementById('name');</script>`, { headers: { "content-type": "text/html" } });
} });
await new Promise(() => {});
}
rmSync(fixedFlag, { force: true });
const fixture = Bun.spawn([process.execPath, import.meta.path, "serve"], { stdout: "ignore", stderr: "inherit" });
await until(async () => { try { return (await fetch("http://127.0.0.1:8000")).ok; } catch { return false; } }, "fixture did not start");

const mode = process.argv[2] || "all";
try {
  if (mode === "persisted" || mode === "isolated") {
    cli("open", "http://127.0.0.1:8000");
    const cookies = cli("eval", "document.cookie");
    assert.equal(cookies.includes("dev_ui_probe=project-one"), mode === "persisted", cookies);
    if (mode === "persisted") assert(readdirSync(artifacts).some(f => f.endsWith(".png")));
    cli("close");
    console.log(`Browser ${mode}: cookies and artifacts checked.`);
  } else {
    assert(ui("status", "browser").includes("stopped"));
    assert(ui("status", "desktop").includes("stopped"));
    assert(existsSync(join(process.env.HOME!, ".agents/skills/playwright-cli/SKILL.md")));
    assert(existsSync(join(process.env.CLAUDE_CONFIG_DIR!, "skills/playwright-cli/SKILL.md")));
    const config = readFileSync(join(process.env.HOME!, ".playwright/cli.config.json"), "utf8");
    cli("open", "http://127.0.0.1:8000");
    cli("tracing-start");
    cli("video-start");
    cli("recording-start");
    function refs() {
      cli("snapshot");
      const latest = readdirSync(artifacts).filter(f => /^page-.*\.yml$/.test(f)).sort().at(-1)!;
      const snapshot = readFileSync(join(artifacts, latest), "utf8");
      const input = /textbox "Name" \[ref=([a-z0-9]+)\]/.exec(snapshot)?.[1];
      const save = /button "Save" \[ref=([a-z0-9]+)\]/.exec(snapshot)?.[1];
      assert(input && save, snapshot);
      return { input, save };
    }
    let ref = refs();
    cli("fill", ref.input, "Ada"); cli("click", ref.save);
    assert(cli("eval", "document.querySelector('[role=status]').textContent").includes("Saved Ada"));
    assert(cli("console", "error").includes("fixture console failure"));
    assert(cli("requests").includes("/broken"));
    cli("screenshot");
    cli("recording-stop"); cli("video-stop");
    const beforeTabs = cli("tab-list");
    ui("start", "browser"); ui("start", "browser");
    assert.equal(cli("tab-list"), beforeTabs, "viewer start must preserve tabs/navigation");

    // Use the image's matching Playwright library to act as a human viewer client.
    const { chromium } = require("/usr/local/lib/node_modules/@playwright/cli/node_modules/playwright");
    const observer = await chromium.launch({ channel: "chromium", headless: true });
    try {
      const dashboard = await observer.newPage({ viewport: { width: 1600, height: 1100 } });
      await dashboard.goto("http://127.0.0.1:8931");
      await dashboard.getByText("UI smoke", { exact: true }).click();
      await dashboard.waitForFunction(() => (document.querySelector("img.display") as HTMLImageElement)?.naturalWidth > 0);
      await dashboard.getByRole("button", { name: "Enable interactive mode" }).click();
      await dashboard.getByRole("button", { name: "Disable interactive mode" }).waitFor();
      const stream = dashboard.locator("img.display");
      const box = await stream.boundingBox(); assert(box);
      // The fixture positions are read from the target, then clicked through the stream.
      const result = JSON.parse(cli("eval", "JSON.stringify({width:innerWidth,rect:document.getElementById('name').getBoundingClientRect().toJSON()})").split("### Result\n")[1].split("\n###")[0]);
      const { width, rect } = typeof result === "string" ? JSON.parse(result) : result;
      const scale = box.width / width;
      await dashboard.mouse.click(box.x + (rect.x + rect.width / 2) * scale, box.y + (rect.y + rect.height / 2) * scale);
      await dashboard.locator(".screen").focus();
      await dashboard.keyboard.press("ControlOrMeta+A");
      await dashboard.keyboard.type("Viewer");
      await until(() => cli("eval", "document.getElementById('name').value").includes("Viewer"), "dashboard keyboard input failed");
      await dashboard.screenshot({ path: join(artifacts, "dashboard.png") });

      writeFileSync(fixedFlag, "fixed");
      cli("reload"); ref = refs(); cli("fill", ref.input, "Grace"); cli("click", ref.save);
      assert(cli("eval", "document.querySelector('[role=status]').textContent").includes("Saved Grace"));
      assert(cli("eval", "getComputedStyle(document.getElementById('save')).backgroundColor").includes("144, 238, 144"));
      cli("screenshot"); cli("tracing-stop");
      cli("eval", "document.cookie='dev_ui_probe=project-one; Max-Age=86400; Path=/'");
      cli("close");
      ui("start", "browser");
      assert(cli("tab-list").includes("about:blank"), "healthy viewer must reopen a closed browser");
      cli("close");
      ui("stop", "browser");
      const occupied = Bun.serve({ port: 8931, hostname: "127.0.0.1", fetch: () => new Response("occupied") });
      assert.notEqual(command(["dev-ui", "start", "browser"], false).code, 0);
      occupied.stop(true);
      ui("start", "browser");
      const pidfile = `/tmp/dev-ui-${process.getuid!()}/browser.pid`;
      command(["kill", "-KILL", "--", `-${readFileSync(pidfile, "utf8").split(" ")[0]}`]);
      await Bun.sleep(100); ui("start", "browser"); ui("stop", "browser"); cli("close");

      ui("start", "desktop");
      const pidBefore = readFileSync(`/tmp/dev-ui-${process.getuid!()}/desktop.pid`, "utf8");
      ui("start", "desktop");
      assert.equal(readFileSync(`/tmp/dev-ui-${process.getuid!()}/desktop.pid`, "utf8"), pidBefore);
      const terminal = Bun.spawn(["dev-ui", "run", "--", "xterm", "-title", "DEV-UI-SMOKE", "-geometry", "80x24+0+0", "-e", "bash", "--noprofile", "--norc"], { stdout: "ignore", stderr: "pipe" });
      const xdo = (...args: string[]) => command(["env", "DISPLAY=:99", "xdotool", ...args]);
      await until(() => command(["env", "DISPLAY=:99", "xdotool", "search", "--name", "DEV-UI-SMOKE"], false).code === 0, "terminal not visible");
      const window = xdo("search", "--name", "DEV-UI-SMOKE").out.trim();
      xdo("windowactivate", "--sync", window);
      xdo("type", "--clearmodifiers", "printf desktop-ok > /tmp/dev-ui-typed"); xdo("key", "Return");
      await until(() => existsSync("/tmp/dev-ui-typed"), "xdotool input failed");
      assert.equal(readFileSync("/tmp/dev-ui-typed", "utf8"), "desktop-ok");
      const desktop = await observer.newPage({ viewport: { width: 1600, height: 1100 } });
      await desktop.goto("http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=remote");
      await desktop.waitForFunction(() => document.querySelector("canvas")?.width === 1440);
      await desktop.locator("canvas").click({ position: { x: 200, y: 180 } });
      await desktop.keyboard.type("printf viewer-ok > /tmp/dev-ui-viewer-typed"); await desktop.keyboard.press("Enter");
      await until(() => existsSync("/tmp/dev-ui-viewer-typed"), "noVNC keyboard input failed");
      assert.equal(readFileSync("/tmp/dev-ui-viewer-typed", "utf8"), "viewer-ok");
      const screenshot = ui("screenshot").trim();
      assert(readFileSync(screenshot).subarray(1, 4).equals(Buffer.from("PNG")));
      await desktop.screenshot({ path: join(artifacts, "novnc.png") });
      ui("stop", "desktop"); await terminal.exited;
      assert(ui("status", "desktop").includes("stopped"));
      ui("start", "desktop");
      command(["pkill", "-x", "Xvfb"]); await Bun.sleep(300);
      ui("start", "desktop"); ui("stop", "desktop");
      assert(command(["pgrep", "-f", "^(/usr/bin/python3 /usr/bin/websockify|Xvfb :99|openbox --sm-disable|x11vnc -display :99)"], false).code !== 0, "orphan UI processes");
    } finally { await observer.close(); }
    assert.equal(readFileSync(join(process.env.HOME!, ".playwright/cli.config.json"), "utf8"), config);
    const files = readdirSync(artifacts, { recursive: true }).map(String);
    assert(files.some(f => f.endsWith(".trace")) && files.some(f => f.endsWith(".network")), "trace missing");
    assert(files.some(f => /\.(webm|mp4)$/.test(f)), "video missing");
    console.log("UI smoke passed: CLI flow/errors/fix/screenshots/trace/video, live dashboard and noVNC input, desktop, idempotence, failures and cleanup.");
  }
} finally { fixture.kill(); await fixture.exited; }
