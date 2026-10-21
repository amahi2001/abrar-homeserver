// Deterministic parity probe; accepts only the pilot's deliberately fake credentials.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { join } from "node:path";

const home = process.env.HOME!, codex = process.env.CODEX_HOME!, claude = process.env.CLAUDE_CONFIG_DIR!;
const hash = (data: string | Buffer) => createHash("sha256").update(data).digest("hex");
const digest = (path: string) => hash(readFileSync(path));
const text = (path: string) => readFileSync(path, "utf8");
function version(command: string) {
  const r = Bun.spawnSync([command, "--version"], { stdout: "pipe", stderr: "pipe" });
  assert.equal(r.exitCode, 0, `${command} --version failed`);
  return (r.stdout.toString() + r.stderr.toString()).trim();
}
function fakeAuth(path: string, expected: string, json = true) {
  const contents = text(path);
  let normalized = contents.trimEnd();
  if (json) {
    try { normalized = JSON.stringify(JSON.parse(contents)); }
    catch { throw Error("Refusing malformed pilot credentials"); }
  }
  // A boolean assertion prevents error formatting from exposing an unexpected secret.
  assert.equal(normalized === expected, true, "Refusing non-pilot credentials");
  assert.equal(statSync(path).mode & 0o777, 0o600, "Pilot credentials must have mode 600");
  return hash(contents);
}
function marker(path: string) {
  if (!existsSync(path)) return false;
  assert.equal(text(path).trimEnd() === "pilot-persistence", true, "Unexpected persistence marker");
  return true;
}
assert.equal(process.env.DEV_CONTAINER, "1");
assert.equal(process.getuid!(), Number(process.env.DEV_UID));
assert.equal(process.getgid!(), Number(process.env.DEV_GID));
assert.equal(existsSync("/var/run/docker.sock"), false);
assert.equal(existsSync("/tmp/dev-ready"), true);
const rules = text(join(codex, "AGENTS.md"));
assert(rules.includes("agent-config:start") && rules.includes("/opt/dev/ui.md"), "Shared instructions missing");
assert(text(join(claude, "CLAUDE.md")).split("\n").includes(`@${codex}/AGENTS.md`), "Claude must import the shared instructions");
const lock = JSON.parse(text("/opt/agent-config/skills-lock.json"));
const skills = [...Object.keys(lock.skills), "playwright-cli"].sort().map(name => {
  const canonical = join(home, ".agents/skills", name);
  assert.equal(realpathSync(join(claude, "skills", name)), realpathSync(canonical), `Claude skill link differs: ${name}`);
  return { name, sha256: digest(join(canonical, "SKILL.md")) };
});
const codexHooks = text(join(codex, "hooks.json")), claudeHooks = text(join(claude, "settings.json"));
assert(codexHooks.includes("rtk hook codex"), "Codex RTK hook missing");
assert(claudeHooks.includes("rtk hook claude"), "Claude RTK hook missing");
const browser = JSON.parse(text(join(home, ".playwright/cli.config.json"))).browser;
assert.equal(browser.browserName, "chromium");
assert.equal(browser.launchOptions.headless, true);
console.log(JSON.stringify({
  uid: process.getuid!(), gid: process.getgid!(),
  tools: Object.fromEntries(["bun", "node", "python3", "uv", "rtk", "codex", "claude", "playwright-cli"].map(tool => [tool, version(tool)])),
  frontendSha256: digest("dist/app.js"),
  skills, instructionsSha256: hash(rules),
  hooks: { codex: hash(codexHooks), claude: hash(claudeHooks) },
  browser: { name: browser.browserName, headless: browser.launchOptions.headless },
  fakeAuth: {
    codex: fakeAuth(join(codex, "auth.json"), '{"pilot":"codex-fake-no-login"}'),
    claude: fakeAuth(join(claude, ".credentials.json"), '{"pilot":"claude-fake-no-login"}'),
    github: fakeAuth("/run/dev-credentials/github-token", "pilot-fake-no-access", false),
  },
  markers: { source: marker("pilot-persistence"), home: marker(join(home, ".cache/pilot-persistence")), account: marker(join(codex, "pilot-persistence")) },
}));
