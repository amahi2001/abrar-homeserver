#!/usr/bin/env bun
// Host orchestration only. All application builds and UI checks run in the boxes.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync, readFileSync, existsSync, cpSync, realpathSync, chmodSync, rmSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

const assets = resolve(import.meta.dir, "..");
const state = join(homedir(), ".local/state/devcontainer-pilot");
const cli = join(homedir(), ".local/share/devcontainer-pilot/cli/bin/devcontainer");
const image = process.env.PILOT_IMAGE;
assert(image?.startsWith("buildfleet-dev:"), "Set PILOT_IMAGE to the current cached base image (see dev project.json baseImage).");
const snapshot = realpathSync(process.env.DEV_AGENT_CONFIG || join(homedir(), ".local/share/agent-config/export"));
const quote = (s: string) => "'" + s.replaceAll("'", "'\\''") + "'";
const digest = (s: string | Buffer) => createHash("sha256").update(s).digest("hex");
function run(args: string[], env: Record<string, string> = {}, allowFailure = false) {
  const p = Bun.spawnSync(args, { env: { ...process.env, ...env }, stdout: "pipe", stderr: "pipe", stdin: "ignore" });
  const out = p.stdout.toString().trim(), err = p.stderr.toString().trim();
  if (!allowFailure) assert.equal(p.exitCode, 0, `${args.join(" ")}\n${out}\n${err}`);
  return { out, err, code: p.exitCode };
}
mkdirSync(state, { recursive: true, mode: 0o700 });
if (!process.env.DEV_PILOT_LOCKED) {
  const child = Bun.spawnSync(["flock", "-n", "-E", "73", join(state, "comparison.lock"), process.execPath, import.meta.path], {
    env: { ...process.env, DEV_PILOT_LOCKED: "1" }, stdout: "inherit", stderr: "inherit", stdin: "ignore",
  });
  process.exit(child.exitCode);
}
assert.equal(run([cli, "--version"]).out, "0.89.0", "Pilot uses the recorded official CLI version.");
const imageId = run(["docker", "image", "inspect", image, "--format", "{{.Id}}"]).out;
function production() {
  const ids = run(["docker", "ps", "-aq", "--filter", "label=dev.managed=true"]).out.split("\n").filter(Boolean);
  if (!ids.length) return [];
  return JSON.parse(run(["docker", "inspect", ...ids]).out)
    .filter((m: any) => !["current-pilot", "standard-pilot"].includes(m.Config.Labels["dev.project"]))
    .map((m: any) => `${m.Id} ${m.Image} ${m.State.StartedAt} ${m.RestartCount}`).sort();
}
const before = production();
const arms = ["current", "standard"].map(kind => {
  const root = join(state, kind), project = `${kind}-pilot`, source = join(root, "projects", project);
  const stateRoot = join(root, "state"), projectState = join(stateRoot, "projects", project);
  const compose = kind === "current" ? `dev-${project}-${digest(source).slice(0, 8)}` : "devcontainers-standard-pilot";
  for (const p of [source, join(projectState, "home"), "accounts/codex", "accounts/claude", "credentials", "bin", "empty-host-auth"].map(p => p.startsWith(root) ? p : join(stateRoot, p))) mkdirSync(p, { recursive: true, mode: 0o700 });
  cpSync(join(import.meta.dir, "fixture"), source, { recursive: true });
  cpSync(join(assets, "ui-smoke.ts"), join(source, "ui-smoke.ts"));
  if (!existsSync(join(source, ".git"))) {
    run(["git", "-C", source, "init", "-b", "agent/pilot"]);
    run(["git", "-C", source, "remote", "add", "origin", "https://github.com/pilot-fixture/no-network.git"]);
    run(["git", "-C", source, "add", "."]);
    run(["git", "-C", source, "-c", "user.name=Pilot", "-c", "user.email=pilot@example.invalid", "commit", "-m", "Local comparison fixture"]);
  }
  const seed = (path: string, content: string) => {
    if (!existsSync(path)) writeFileSync(path, content, { mode: 0o600 });
    assert.equal(readFileSync(path, "utf8") === content, true, "Refusing non-pilot credentials");
    chmodSync(path, 0o600);
  };
  seed(join(stateRoot, "accounts/codex/auth.json"), '{"pilot":"codex-fake-no-login"}\n');
  seed(join(stateRoot, "accounts/claude/.credentials.json"), '{"pilot":"claude-fake-no-login"}\n');
  seed(join(stateRoot, "credentials/github-token"), "pilot-fake-no-access\n");
  // Refuse every GitHub operation except local fake-token retrieval.
  writeFileSync(join(stateRoot, "bin/gh"), '#!/bin/sh\nif [ "$*" = "auth token --hostname github.com" ]; then echo pilot-fake-no-access; else echo "Pilot blocks GitHub operations" >&2; exit 1; fi\n', { mode: 0o700 });
  const env: Record<string, string> = {
    DEV_ASSETS: assets, DEV_ROOT: join(root, "projects"), DEV_STATE_ROOT: stateRoot,
    DEV_AGENT_CONFIG: snapshot, CODEX_HOME: join(stateRoot, "empty-host-auth"), CLAUDE_CONFIG_DIR: join(stateRoot, "empty-host-auth"),
    PATH: `${join(stateRoot, "bin")}:${process.env.PATH}`, DEV_IMAGE: image, DEV_PROJECT: project,
    DEV_SOURCE: source, DEV_PROJECT_STATE: projectState, DEV_ACCOUNTS: join(stateRoot, "accounts"),
    DEV_CREDENTIALS: join(stateRoot, "credentials"), DEV_UID: String(process.getuid!()), DEV_GID: String(process.getgid!()),
    DEV_GIT_NAME: "Pilot", DEV_GIT_EMAIL: "pilot@example.invalid", COMPOSE_PROJECT_NAME: compose,
    GIT_CONFIG_COUNT: "2", GIT_CONFIG_KEY_0: "user.name", GIT_CONFIG_VALUE_0: "Pilot", GIT_CONFIG_KEY_1: "user.email", GIT_CONFIG_VALUE_1: "pilot@example.invalid",
  };
  const native = ["--workspace-folder", source, "--config", join(import.meta.dir, "devcontainer.json"), "--id-label", "devcontainers.pilot=standard", "--user-data-folder", join(root, "cli-state")];
  const custom = [process.execPath, join(assets, "dev.ts")];
  if (kind === "current" && !existsSync(join(projectState, "project.json"))) writeFileSync(join(projectState, "project.json"), JSON.stringify({ repo: "pilot-fixture/no-network", privateDefault: false, source, composeProject: compose, image: `${image}-${compose}`, baseImage: image }));
  writeFileSync(join(state, `${kind}.env`), Object.entries(env).map(([k,v]) => `export ${k}=${quote(v)}`).join("\n") + "\n", { mode: 0o600 });
  writeFileSync(join(state, `${kind}-exec`), `#!/usr/bin/env bash\nset -euo pipefail\nsource ${quote(join(state, `${kind}.env`))}\n` + (kind === "current" ? `exec ${custom.map(quote).join(" ")} shell ${project} -- "$@"\n` : `exec ${quote(cli)} exec ${native.map(quote).join(" ")} "$@"\n`), { mode: 0o700 });
  const container = () => run(["docker", "ps", "-aq", "--filter", `label=com.docker.compose.project=${compose}`, "--filter", "label=com.docker.compose.service=dev"]).out;
  const inside = (args: string[]) => run(["docker", "exec", "--user", `${env.DEV_UID}:${env.DEV_GID}`, container(), ...args], env);
  async function up() {
    const start = performance.now();
    const r = kind === "current" ? run([...custom, "start", project, "--no-agent"], env, true) : run([cli, "up", ...native], env, true);
    writeFileSync(join(state, `${kind}-last-up.log`), r.out + "\n" + r.err);
    assert.equal(r.code, 0, `${kind} startup failed: ${r.out}\n${r.err}`);
    for (let i = 0; i < 150; i++) {
      const id = container();
      if (id && run(["docker", "inspect", id, "--format", '{{.State.Health.Status}}'], {}, true).out === "healthy") {
        const meta = JSON.parse(run(["docker", "inspect", id]).out)[0];
        assert.equal(meta.Image, imageId, "Both arms must use precisely the same cached image.");
        assert(Object.values(meta.NetworkSettings.Ports).every((bindings: any) => bindings === null || bindings.every((b: any) => b.HostIp === "127.0.0.1")));
        assert.equal(meta.NetworkSettings.Ports["5900/tcp"], undefined);
        return (performance.now() - start) / 1000;
      }
      await Bun.sleep(200);
    }
    throw Error(`${kind} not healthy: ${run(["docker", "logs", container()], {}, true).out}`);
  }
  function stop(remove = false) {
    const id = container(); if (!id) return;
    if (run(["docker", "inspect", id, "--format", "{{.State.Running}}"]).out === "true") inside(["dev-ui", "_checkpoint"]);
    run(["docker", remove ? "rm" : "stop", ...(remove ? ["-f"] : []), id]);
  }
  return { kind, source, root, env, container, inside, up, stop, exec: (args: string[]) => run([join(state, `${kind}-exec`), ...args]), samples: { reuse: [] as number[], restart: [] as number[], recreate: [] as number[] } };
});

const result: any = { recordedAt: new Date().toISOString(), image, imageId, snapshotDigest: digest(readFileSync(join(snapshot, "tools.lock.json"))), cliVersion: "0.89.0", samples: {}, outputs: {} };
for (const arm of arms) {
  console.log(`Preparing ${arm.kind}…`);
  result.outputs[arm.kind] = { initialReadySeconds: await arm.up() };
  arm.exec(["true"]);
  arm.inside(["dev-ui", "stop", "browser"]); arm.inside(["dev-ui", "stop", "desktop"]);
  arm.inside(["playwright-cli", "close-all"]);
  // Only disposable pilot profiles are reset; recreation below tests persistence.
  const uiHome = join(arm.env.DEV_PROJECT_STATE, "home/.local/share/dev-ui");
  rmSync(join(uiHome, "browser-profile"), { recursive: true, force: true });
  rmSync(join(uiHome, "browser-state.json"), { force: true });
}
// Alternate order to reduce cache/order bias. Measure full command plus health readiness.
for (const phase of ["reuse", "restart", "recreate"] as const) {
  for (let i = 0; i < 6; i++) for (const arm of i % 2 ? [...arms].reverse() : arms) {
    if (phase !== "reuse") arm.stop(phase === "recreate");
    arm.samples[phase].push(await arm.up());
  }
  console.log(`Measured ${phase}: six samples per implementation.`);
  result.samples = Object.fromEntries(arms.map(arm => [arm.kind, arm.samples]));
  writeFileSync(join(state, "results.partial.json"), JSON.stringify(result, null, 2) + "\n");
}
for (const arm of arms) {
  const output = result.outputs[arm.kind];
  arm.inside(["bash", "-ec", 'echo pilot-persistence > pilot-persistence; echo pilot-persistence > "$HOME/.cache/pilot-persistence"; echo pilot-persistence > "$CODEX_HOME/pilot-persistence"; grep -q "Pilot learned rule" "$CODEX_HOME/AGENTS.md" || echo "Pilot learned rule." >> "$CODEX_HOME/AGENTS.md"']);
  const build = arm.exec(["bun", "run", "build"]);
  writeFileSync(join(state, `${arm.kind}-build.log`), build.out + "\n" + build.err);
  const test = arm.exec(["bun", "test"]);
  writeFileSync(join(state, `${arm.kind}-test.log`), test.out + "\n" + test.err);
  output.probe = JSON.parse(arm.exec(["bun", "probe.ts"]).out);
  output.build = output.probe.frontendSha256;
  output.test = /\d+ pass/.exec(test.err)?.[0];
  assert.equal(output.test, "1 pass");
  output.frontend = JSON.parse(arm.exec(["bun", "verify-ui.ts"]).out);
  output.discovery = JSON.parse(arm.exec(["bun", "discover.ts"]).out);
  writeFileSync(join(state, `${arm.kind}-outputs.json`), JSON.stringify(output, null, 2) + "\n");
  // Other projects must never see a profile or cookie before creating their own.
  arm.inside(["bun", "ui-smoke.ts", "isolated"]);
  console.log(`Running browser/desktop checks in ${arm.kind}…`);
  const ui = arm.inside(["bun", "ui-smoke.ts"]);
  writeFileSync(join(state, `${arm.kind}-ui.log`), ui.out + "\n" + ui.err);
  output.ui = ui.out;
  arm.inside(["bash", "-ec", 'stat -c %Y "$HOME/.local/state/dev-playwright-version" > "$HOME/skill-install-time"']);
  arm.stop(true); await arm.up();
  output.persistence = arm.inside(["bun", "ui-smoke.ts", "persisted"]).out;
  arm.inside(["bash", "-ec", 'grep -q "Pilot learned rule" "$CODEX_HOME/AGENTS.md"; test "$(stat -c %Y "$HOME/.local/state/dev-playwright-version")" = "$(cat "$HOME/skill-install-time")"']);
  assert.deepEqual(JSON.parse(arm.exec(["bun", "probe.ts"]).out), output.probe, "Tools/auth/skills must survive recreation.");
  arm.inside(["dev-ui", "stop", "browser"]); arm.inside(["dev-ui", "stop", "desktop"]);
  const meta = JSON.parse(run(["docker", "inspect", arm.container()]).out)[0];
  output.container = arm.container(); output.ports = meta.NetworkSettings.Ports;
  output.viewers = {};
  for (const [service, port, path] of [["browser", "8931", "/"], ["desktop", "6080", "/vnc.html"]]) {
    arm.exec(["dev-ui", "start", service]);
    const mapping = output.ports[`${port}/tcp`][0];
    assert.equal(mapping.HostIp, "127.0.0.1");
    const response = await fetch(`http://127.0.0.1:${mapping.HostPort}${path}`, { signal: AbortSignal.timeout(5000) });
    assert.equal(response.status, 200, `${service} viewer not accessible through published loopback port`);
    output.viewers[service] = { httpStatus: response.status, host: mapping.HostIp };
    arm.exec(["dev-ui", "stop", service]);
  }
  arm.inside(["playwright-cli", "close-all"]);
  result.samples[arm.kind] = arm.samples;
}
assert.deepEqual(result.outputs.current.probe, result.outputs.standard.probe, "Normalized environment outputs differ.");
assert.equal(result.outputs.current.build, result.outputs.standard.build, "Build outputs differ.");
assert.equal(result.outputs.current.test, result.outputs.standard.test, "Test outputs differ.");
assert.deepEqual(result.outputs.current.frontend, result.outputs.standard.frontend, "Rendered frontend outputs differ.");
assert.deepEqual(result.outputs.current.discovery, result.outputs.standard.discovery, "Native harness skill discovery differs.");
assert.deepEqual(production(), before, "Production containers changed during pilot.");
writeFileSync(join(state, "results.json"), JSON.stringify(result, null, 2) + "\n");
console.log(`Equal build, test and environment outputs; both UI/persistence suites passed. Results: ${state}/results.json`);
console.log(`Shells: ${state}/current-exec bash | ${state}/standard-exec bash`);
