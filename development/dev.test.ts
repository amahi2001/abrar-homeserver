import { test, expect, afterEach } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, statSync, copyFileSync, symlinkSync, unlinkSync, chmodSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { projectName, repository, githubRemote, withAgentDispatchLock } from "./dev";
import "./codebase-memory.test";

const roots: string[] = [];
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });
const realGit = Bun.which("git")!;
const script = process.env.DEV_TEST_SCRIPT || join(import.meta.dir, "dev.ts");

function fixture() {
  const dir = mkdtempSync(join(tmpdir(), "dev-test-")); roots.push(dir);
  const bin = join(dir, "bin"), source = join(dir, "projects/app"), state = join(dir, "state");
  mkdirSync(bin);
  const assets = join(dir, "assets");
  mkdirSync(join(assets, "skills/ponytail"), { recursive: true });
  for (const file of ["Dockerfile", "entrypoint.sh", "agent-session.sh", "dev-finish.sh", "gh.sh", "instructions.md", "dev-ui.sh", "ui.md", "playwright-cli.sh", "browser-state.cjs", "codebase-memory.ts"]) copyFileSync(join(import.meta.dir, file), join(assets, file));
  writeFileSync(join(assets, "skills/ponytail/SKILL.md"), "test fixture skill");
  mkdirSync(join(dir, "host-codex"));
  writeFileSync(join(dir, "host-codex/auth.json"), '{"test":"fake-codex-credential"}');
  mkdirSync(join(dir, "host-claude"));
  writeFileSync(join(dir, "host-claude/.credentials.json"), '{"claudeAiOauth":{"accessToken":"fake-claude-credential","refreshToken":"fake-claude-refresh"}}');
  const mock = `#!${process.execPath}
import {appendFileSync,existsSync,writeFileSync,readFileSync,mkdirSync,copyFileSync} from 'node:fs';
const name=process.argv[1].split('/').pop(), a=process.argv.slice(2);
appendFileSync(process.env.FAKE_LOG,JSON.stringify({name,args:a,agentConfig:process.env.DEV_AGENT_CONFIG})+'\\n');
const fail=(message)=>{console.error(message);process.exit(1)};
if(name==='git') {
 if(a.includes('ls-remote')) process.exit(0);
 if(a.includes('push')) {if(process.env.FAKE_PUSH_FAIL)fail('network unavailable');process.exit(0)}
 const r=Bun.spawnSync([process.env.REAL_GIT,...a],{stdout:'inherit',stderr:'inherit',stdin:'ignore'});process.exit(r.exitCode);
}
if(name==='gh') {
 if(a[0]==='auth'&&a[1]==='token') {console.log('fake-github-credential');process.exit(0)}
 if(a[0]==='api'&&a[1]==='user') {console.log(JSON.stringify({login:'amahi2001'}));process.exit(0)}
 if(a[0]==='repo'&&a[1]==='create') {writeFileSync(process.env.FAKE_CREATED,'yes');process.exit(0)}
 if(a[0]==='api') {
  if(process.env.FAKE_NETWORK_FAIL)fail('connection refused');
  if(process.env.FAKE_MISSING&&!existsSync(process.env.FAKE_CREATED))fail('gh: Not Found (HTTP 404)');
  console.log(JSON.stringify({full_name:a[1].slice(6),private:!process.env.FAKE_PUBLIC}));process.exit(0);
 }
 fail('unexpected gh invocation');
}
if(name==='docker') {
 if(a[0]==='info')console.log('29.8.1');
 if(a.includes('config'))console.log(JSON.stringify({services:{dev:{image:process.env.DEV_IMAGE,build:process.env.FAKE_CUSTOM_BUILD?{context:process.env.DEV_SOURCE}:undefined,environment:{snapshot:process.env.DEV_AGENT_CONFIG},volumes:[{type:'bind',source:process.env.DEV_PROJECT_STATE+'/home',target:'/home/node'}]},...(process.env.FAKE_SIDECAR?{db:{image:'cached-db'}}:{})}}));
 if(a[0]==='inspect'&&a.includes('{{json .Mounts}}')) {console.log(JSON.stringify([{Type:'bind',Source:process.env.FAKE_HOME_UNKNOWN||process.env.DEV_STATE_ROOT+'/projects/app/home',Destination:'/home/node'}]));process.exit(0)}
 if(a[0]==='inspect') {if(a.includes('{{.State.Health.Status}}'))console.log(process.env.FAKE_HEALTH_FAIL?'unhealthy':'healthy');else console.log(JSON.stringify({Running:!process.env.FAKE_HEALTH_FAIL,Health:{Status:process.env.FAKE_HEALTH_FAIL?'unhealthy':'healthy'}}));process.exit(0)}
 if(a.includes('inspect')&&process.env.FAKE_IMAGE_MISSING)fail('image absent');
 if(a.includes('inspect')&&process.env.FAKE_WORKSPACE_MISSING&&a.at(-1).includes('-dev-'))fail('workspace image absent');
 if(a.includes('ps')&&a.includes('-q')&&process.env.FAKE_RUNNING)console.log('container-id');
 if(a.includes('port'))console.log(process.env.FAKE_PORT || '127.0.0.1:43219');
 if(a.includes('flock')&&process.env.FAKE_AGENT_ACTIVE)fail('locked');
 if(a[0]==='build'&&process.env.FAKE_CHECK_BUILD_LOCK&&Bun.spawnSync(['flock','-n',process.env.DEV_DEPLOYMENT_LOCK,'true']).exitCode===0)fail('image build did not hold host deployment lock');
 if(a[0]==='build'&&process.env.FAKE_AGENT_DURING_BUILD) {
  const marker=process.env.FAKE_AGENT_DURING_BUILD, lock=process.env.DEV_STATE_ROOT+'/projects/app/home/.agent-session.lock';
  const child=Bun.spawn(['flock','--no-fork',lock,process.execPath,'-e','await Bun.write('+JSON.stringify(marker)+', String(process.pid));await Bun.sleep(10000)'],{stdin:'ignore',stdout:'ignore',stderr:'ignore'});child.unref();
  for(let i=0;i<100&&!existsSync(marker);i++)await Bun.sleep(10);
  if(!existsSync(marker))fail('test agent could not acquire dispatch lock');
 }
 process.exit(0);
}
if(name==='devcontainer') {
 if(a[0]==='--version') {console.log(existsSync(process.env.FAKE_INSTALLED)?readFileSync(process.env.FAKE_INSTALLED,'utf8'):process.env.FAKE_CLI_VERSION||'0.89.0');process.exit(0)}
 if(a[0]==='up') {if(process.env.FAKE_UP_FAIL)fail('container unhealthy');console.log(JSON.stringify({containerId:'container-id'}));}
 if(process.env.FAKE_CHECK_DISPATCH_LOCK&&(a[0]==='build'||a[0]==='up'||a.includes('dev-agent'))) {
  const held=Bun.spawnSync(['flock','-n',process.env.DEV_STATE_ROOT+'/projects/app/home/.agent-session.lock','true']).exitCode!==0;
  appendFileSync(process.env.FAKE_LOG,JSON.stringify({name:'dispatch-lock',phase:a.includes('dev-agent')?'agent':a[0],held})+'\\n');
  if(held===a.includes('dev-agent'))fail('incorrect dispatch lock lifetime');
  const hostHeld=Bun.spawnSync(['flock','-n',process.env.DEV_DEPLOYMENT_LOCK,'true']).exitCode!==0;
  if(hostHeld===a.includes('dev-agent'))fail('incorrect host deployment lock lifetime');
 }
 if(a.includes('dev-agent')&&process.env.FAKE_AGENT_STARTED) {writeFileSync(process.env.FAKE_AGENT_STARTED,'ready');await Bun.sleep(600);}
 if(a.some(value=>value.includes('dev-ui'))&&process.env.FAKE_UI_FAIL)fail('viewer failed');
 if(a.includes('flock')&&process.env.FAKE_AGENT_ACTIVE)fail('locked');
 process.exit(0);
}
if(name==='curl') {writeFileSync(a[a.indexOf('-o')+1],'# fake official installer');process.exit(0)}
if(name==='sh') {writeFileSync(process.env.FAKE_INSTALLED,a[a.indexOf('--version')+1]);const p=a[a.indexOf('--prefix')+1]+'/node/current/bin';mkdirSync(p,{recursive:true});copyFileSync(process.argv[1],p+'/node');process.exit(0)}
if(name==='node') {console.log(process.env.FAKE_NODE_VERSION||'v24.22.1');process.exit(0)}
fail('unexpected mock');
`;
  for (const name of ["git", "gh", "docker", "devcontainer", "curl", "sh"]) writeFileSync(join(bin, name), mock, { mode: 0o755 });
  const env = {
    ...process.env, PATH: `${bin}:${process.env.PATH}`, REAL_GIT: realGit,
    DEV_ROOT: join(dir, "projects"), DEV_STATE_ROOT: state, CODEX_HOME: join(dir, "host-codex"),
    CLAUDE_CONFIG_DIR: join(dir, "host-claude"),
    DEV_ASSETS: assets,
    DEVCONTAINER_CLI: join(bin, "devcontainer"),
    DEV_LOCK_HELD: "", DEV_HOST_LOCK_HELD: "", DEV_DEPLOYMENT_LOCK: join(dir, "host-deployment.lock"),
    FAKE_LOG: join(dir, "calls.jsonl"), FAKE_CREATED: join(dir, "created"), FAKE_INSTALLED: join(dir, "installed"),
    GIT_AUTHOR_NAME: "Test", GIT_AUTHOR_EMAIL: "test@example.invalid", GIT_COMMITTER_NAME: "Test", GIT_COMMITTER_EMAIL: "test@example.invalid",
  };
  return {
    dir, source, state, env,
    cli(args: string[], extra: Record<string, string> = {}) {
      const r = Bun.spawnSync([process.execPath, script, ...args], { env: { ...env, ...extra }, stdout: "pipe", stderr: "pipe" });
      return { code: r.exitCode, out: r.stdout.toString(), err: r.stderr.toString() };
    },
    git(...args: string[]) {
      const r = Bun.spawnSync([realGit, "-C", source, ...args], { env, stdout: "pipe", stderr: "pipe" });
      if(r.exitCode) throw Error(r.stderr.toString());
      return r.stdout.toString().trim();
    },
    calls() { const rows = existsSync(env.FAKE_LOG) ? readFileSync(env.FAKE_LOG, "utf8").trim() : ""; return rows ? rows.split("\n").map(line => JSON.parse(line)) : []; },
    clear() { writeFileSync(env.FAKE_LOG, ""); },
  };
}

test("rejects path traversal, URLs, shell strings, and malformed project/repository names", () => {
  for (const bad of ["../escape", "a/b", "-flag", "My App", "x;touch y", "a\nb"]) expect(() => projectName(bad)).toThrow();
  for (const bad of ["https://github.com/a/b", "../repo", "a/..", "a/b/c", "a/b;foo"]) expect(() => repository(bad)).toThrow();
  expect(githubRemote("git@github.com:owner/repo.git")).toBe("owner/repo");
  expect(githubRemote("https://github.com/owner/repo.git")).toBe("owner/repo");
});

test("view validates arguments, initialized/running state and loopback mappings", () => {
  const f = fixture();
  for (const args of [["browser", "extra"], ["--agent", "claude"], ["desktop", "--stop", "--stop"], ["--"], ["typo"]]) {
    expect(f.cli(["view", "app", ...args]).err).toContain("Usage: dev view");
  }
  expect(f.cli(["view", "app"]).err).toContain("not initialized");
  expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  expect(f.cli(["view", "app"]).err).toContain("not running");
  for (const port of ["0.0.0.0:43219", "[::]:43219", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:1\n0.0.0.0:1"]) {
    f.clear();
    expect(f.cli(["view", "app"], { FAKE_RUNNING: "1", FAKE_PORT: port }).code).toBe(1);
    expect(f.calls().some(c => c.args.includes("dev-ui"))).toBe(false);
  }
});

test("view discovers the published port and controls services without changing checkout or recreating", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const branch = f.git("branch", "--show-current");
  f.clear();
  const r = f.cli(["view", "app"], { FAKE_RUNNING: "1", FAKE_AGENT_ACTIVE: "1", DEV_SSH_TARGET: "me@server" });
  expect(r.code).toBe(0);
  expect(r.out).toContain("http://127.0.0.1:43219/");
  expect(r.out).toContain("ssh -N -L 43219:127.0.0.1:43219 'me@server'");
  expect(f.calls().some(c => c.args.slice(-3).join(" ") === "dev-ui start browser")).toBe(true);
  expect(f.calls().some(c => c.args.includes("up") || c.args.includes("flock") || c.args.includes("switch"))).toBe(false);
  f.clear();
  expect(f.cli(["view", "app", "desktop", "--stop"], { FAKE_RUNNING: "1" }).code).toBe(0);
  expect(f.calls().some(c => c.args.slice(-3).join(" ") === "dev-ui stop desktop")).toBe(true);
  expect(f.calls().some(c => c.args.slice(-3).join(" ") === "port container-id 6080/tcp")).toBe(true);
  expect(f.cli(["view", "app", "desktop"], { FAKE_RUNNING: "1", FAKE_UI_FAIL: "1" }).code).toBe(1);
  expect(f.git("branch", "--show-current")).toBe(branch);
});

test("Playwright lock versions change the image and missing fields retain the pinned default", () => {
  const f = fixture(), snapshot = join(f.dir, "snapshot"); mkdirSync(snapshot);
  const lock = join(snapshot, "tools.lock.json");
  writeFileSync(lock, "{}");
  expect(f.cli(["build"], { DEV_AGENT_CONFIG: snapshot }).code).toBe(0);
  const first = f.calls().find(c => c.args[0] === "build").args;
  expect(first).toContain("PLAYWRIGHT_CLI_VERSION=0.1.22");
  f.clear(); writeFileSync(lock, JSON.stringify({ playwrightCli: { version: "0.1.23" } }));
  expect(f.cli(["build"], { DEV_AGENT_CONFIG: snapshot }).code).toBe(0);
  const second = f.calls().find(c => c.args[0] === "build").args;
  expect(second).toContain("PLAYWRIGHT_CLI_VERSION=0.1.23");
  expect(first[2]).not.toBe(second[2]);
  writeFileSync(lock, JSON.stringify({ playwrightCli: { version: "latest" } }));
  expect(f.cli(["build"], { DEV_AGENT_CONFIG: snapshot }).err).toContain("Invalid locked Playwright");
});

test("Codebase Memory lock and helper changes rebuild the cached image through official installer arguments", () => {
  const f = fixture(), snapshot = join(f.dir, "memory-snapshot"); mkdirSync(snapshot);
  const path = join(snapshot, "tools.lock.json");
  writeFileSync(path, "{}");
  expect(f.cli(["build"], { DEV_AGENT_CONFIG: snapshot }).code).toBe(0);
  const first = f.calls().find(c => c.name === "docker" && c.args[0] === "build").args;
  expect(first).toContain("CODEBASE_MEMORY_VERSION=0.12.1");
  const codebaseMemory = {
    version: "0.12.2",
    installer: "https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/0123456789abcdef0123456789abcdef01234567/install.sh",
    installerSha256: "a".repeat(64),
  };
  const build = () => {
    f.clear();
    expect(f.cli(["build"], { DEV_AGENT_CONFIG: snapshot }).code).toBe(0);
    return f.calls().find(c => c.name === "docker" && c.args[0] === "build").args;
  };
  writeFileSync(path, JSON.stringify({ codebaseMemory }));
  const second = build();
  expect(second).toContain("CODEBASE_MEMORY_VERSION=0.12.2");
  expect(second).toContain(`CODEBASE_MEMORY_INSTALLER=${codebaseMemory.installer}`);
  expect(second).toContain(`CODEBASE_MEMORY_INSTALLER_SHA256=${codebaseMemory.installerSha256}`);
  expect(second[second.indexOf("--tag") + 1]).not.toBe(first[first.indexOf("--tag") + 1]);
  codebaseMemory.installerSha256 = "b".repeat(64);
  writeFileSync(path, JSON.stringify({ codebaseMemory }));
  const third = build();
  expect(third[third.indexOf("--tag") + 1]).not.toBe(second[second.indexOf("--tag") + 1]);
  const helper = join(f.env.DEV_ASSETS, "codebase-memory.ts");
  chmodSync(helper, 0o600);
  writeFileSync(helper, readFileSync(helper, "utf8") + "\n// changed helper fixture\n");
  const fourth = build();
  expect(fourth[fourth.indexOf("--tag") + 1]).not.toBe(third[third.indexOf("--tag") + 1]);
});

test("malformed Codebase Memory locks fail before any image build", () => {
  const f = fixture(), snapshot = join(f.dir, "bad-memory-snapshot"); mkdirSync(snapshot);
  const path = join(snapshot, "tools.lock.json");
  const valid = {
    version: "0.12.1",
    installer: "https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/0123456789abcdef0123456789abcdef01234567/install.sh",
    installerSha256: "a".repeat(64),
  };
  for (const codebaseMemory of [
    { ...valid, version: "latest" }, { ...valid, version: 12 },
    { ...valid, installer: "file:///tmp/installer.sh" }, { ...valid, installer: 42 },
    { ...valid, installerSha256: "" }, { ...valid, installerSha256: "bad-digest" },
  ]) {
    f.clear(); writeFileSync(path, JSON.stringify({ codebaseMemory }));
    const result = f.cli(["build"], { DEV_AGENT_CONFIG: snapshot });
    expect(result.code).toBe(1);
    expect(result.err).toContain("Invalid locked Codebase Memory");
    expect(readFileSync(f.env.FAKE_LOG, "utf8")).toBe("");
  }
});

test("shared Compose publishes both viewers only to loopback and keeps VNC internal", () => {
  const compose = readFileSync(join(import.meta.dir, "compose.yaml"), "utf8");
  expect(compose).toContain('"127.0.0.1::8931"');
  expect(compose).toContain('"127.0.0.1::6080"');
  expect(compose).not.toContain("5900");
});

test("failed browser checkpoint blocks stop/recreation and preserves project metadata", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const path = join(f.state, "projects/app/project.json"), before = readFileSync(path, "utf8");
  for (const command of ["stop", "rebuild", "start"]) {
    f.clear();
    const snapshot = join(f.dir, "changed-snapshot"); mkdirSync(snapshot, { recursive: true });
    const result = f.cli([command, "app", ...(command === "start" ? ["--no-agent"] : [])], { FAKE_RUNNING: "1", FAKE_UI_FAIL: "1", DEV_AGENT_CONFIG: snapshot });
    expect(result.code).toBe(1);
    expect(f.calls().some(c => c.args.some(value => value.includes("dev-ui _checkpoint")))).toBe(true);
    expect(f.calls().some(c => c.args.includes("up") || c.args.at(-1) === "stop")).toBe(false);
    expect(readFileSync(path, "utf8")).toBe(before);
  }
});

test("snapshot changes resolve to new mount paths without building images", () => {
  const f = fixture();
  const one = join(f.dir, "snapshot-one"), two = join(f.dir, "snapshot-two"), link = join(f.dir, "config-export");
  mkdirSync(one); mkdirSync(two); symlinkSync(one, link);
  let result = f.cli(["start", "app", "--no-agent"], { DEV_AGENT_CONFIG: link });
  expect(result.code).toBe(0);
  expect(f.calls().find(c => c.name === "devcontainer" && c.args.includes("up")).agentConfig).toBe(one);
  unlinkSync(link); symlinkSync(two, link); f.clear();
  result = f.cli(["start", "app", "--no-agent"], { DEV_AGENT_CONFIG: link });
  expect(result.code).toBe(0);
  expect(f.calls().find(c => c.name === "devcontainer" && c.args.includes("up")).agentConfig).toBe(two);
  expect(f.calls().some(c => c.name === "docker" && c.args.includes("build"))).toBe(false);
  result = f.cli(["start", "app", "--no-agent"], { DEV_AGENT_CONFIG: join(f.dir, "missing") });
  expect(result.code).toBe(1); expect(result.err).toContain("snapshot not found");
});

test("creates and verifies a private default repo; credentials stay outside source and logs", () => {
  const f = fixture();
  const r = f.cli(["start", "app", "--no-agent"], { FAKE_MISSING: "1" });
  expect(r.code).toBe(0);
  expect(f.calls().some(c => c.name === "gh" && c.args.join(" ") === "repo create amahi2001/app --private")).toBe(true);
  expect(f.git("branch", "--show-current")).toStartWith("agent/work-");
  expect(f.git("ls-files")).toBe(".gitignore\nREADME.md");
  expect(statSync(join(f.state, "credentials/github-token")).mode & 0o777).toBe(0o600);
  expect(readFileSync(join(f.state, "accounts/codex/auth.json"), "utf8")).toContain("fake-codex-credential");
  expect(statSync(join(f.state, "accounts/claude/.credentials.json")).mode & 0o777).toBe(0o600);
  expect(readFileSync(join(f.state, "accounts/claude/.credentials.json"), "utf8")).toContain("fake-claude-credential");
  expect(r.out + r.err + JSON.stringify(f.calls())).not.toContain("fake-github-credential");
  expect(r.out + r.err + JSON.stringify(f.calls())).not.toContain("fake-claude-credential");
  expect(r.out + r.err + JSON.stringify(f.calls())).not.toContain("fake-claude-refresh");
  expect(f.calls().find(c => c.name === "git" && c.args.includes("push"))?.args).toContain("main");
});

test("Claude auth is seeded after a later host login and container refreshes survive subsequent starts", () => {
  const f = fixture(), host = join(f.env.CLAUDE_CONFIG_DIR, ".credentials.json");
  const original = readFileSync(host, "utf8"); unlinkSync(host);
  expect(f.cli(["start", "app", "--agent", "claude", "--no-agent"]).code).toBe(0);
  const saved = join(f.state, "accounts/claude/.credentials.json");
  expect(existsSync(saved)).toBe(false);
  writeFileSync(host, original);
  expect(f.cli(["start", "app", "--agent", "claude", "--no-agent"]).code).toBe(0);
  expect(readFileSync(saved, "utf8")).toBe(original);
  writeFileSync(saved, '{"claudeAiOauth":{"accessToken":"refreshed-container-token"}}');
  writeFileSync(host, '{"claudeAiOauth":{"accessToken":"different-host-token"}}');
  expect(f.cli(["start", "app", "--agent", "claude", "--no-agent"]).code).toBe(0);
  expect(readFileSync(saved, "utf8")).toContain("refreshed-container-token");
  expect(statSync(saved).mode & 0o777).toBe(0o600);
});

test("Claude auth uses the standard home path when CLAUDE_CONFIG_DIR is unset", () => {
  const f = fixture();
  mkdirSync(join(f.dir, ".claude"));
  writeFileSync(join(f.dir, ".claude/.credentials.json"), '{"claudeAiOauth":{"accessToken":"default-path-token"}}');
  expect(f.cli(["start", "app", "--no-agent"], { HOME: f.dir, CLAUDE_CONFIG_DIR: "" }).code).toBe(0);
  expect(readFileSync(join(f.state, "accounts/claude/.credentials.json"), "utf8")).toContain("default-path-token");
});

test("tool lock updates change the image tag and pass versions to official installers", () => {
  const f = fixture(), config = join(f.dir, "tool-snapshot"); mkdirSync(config);
  const path = join(config, "tools.lock.json");
  const lock = { rtk: { version: "1.0.1", installer: "https://example.invalid/official-install.sh", installerSha256: "test-hash" } };
  writeFileSync(path, JSON.stringify(lock));
  expect(f.cli(["build"], { DEV_AGENT_CONFIG: config }).code).toBe(0);
  const first = f.calls().find(c => c.name === "docker" && c.args[0] === "build").args;
  expect(first).toContain("RTK_VERSION=v1.0.1");
  expect(first).toContain("RTK_INSTALLER_SHA256=test-hash");
  lock.rtk.version = "1.0.2"; writeFileSync(path, JSON.stringify(lock)); f.clear();
  expect(f.cli(["build"], { DEV_AGENT_CONFIG: config }).code).toBe(0);
  const second = f.calls().find(c => c.name === "docker" && c.args[0] === "build").args;
  expect(second).toContain("RTK_VERSION=v1.0.2");
  expect(second[second.indexOf("--tag") + 1]).not.toBe(first[first.indexOf("--tag") + 1]);
});

test("warm start preserves uncommitted work and task branch, skips GitHub API and image builds", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const branch = f.git("branch", "--show-current");
  writeFileSync(join(f.source, "work.txt"), "keep this work"); f.clear();
  expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  expect(f.git("branch", "--show-current")).toBe(branch);
  expect(readFileSync(join(f.source, "work.txt"), "utf8")).toBe("keep this work");
  expect(f.calls().some(c => c.name === "gh" && c.args[0] === "api")).toBe(false);
  expect(f.calls().some(c => c.name === "docker" && c.args[0] === "build")).toBe(false);
  expect(f.cli(["start", "app", "--task", "new", "--no-agent"]).code).toBe(1);
});

test("public default collision and network failure never create a replacement repo", () => {
  for (const extra of [{ FAKE_PUBLIC: "1" }, { FAKE_NETWORK_FAIL: "1" }]) {
    const f = fixture(); expect(f.cli(["start", "app", "--no-agent"], extra).code).toBe(1);
    expect(f.calls().some(c => c.name === "gh" && c.args[0] === "repo")).toBe(false);
    expect(existsSync(f.source)).toBe(false);
  }
});

test("missing assigned repo is never replaced; assigned public repo is accepted", () => {
  const f = fixture();
  expect(f.cli(["start", "app", "--repo", "owner/assigned", "--no-agent"], { FAKE_MISSING: "1" }).code).toBe(1);
  expect(f.calls().some(c => c.name === "gh" && c.args[0] === "repo")).toBe(false);
  expect(f.cli(["start", "app", "--repo", "owner/assigned", "--no-agent"], { FAKE_PUBLIC: "1" }).code).toBe(0);
  expect(f.git("remote", "get-url", "origin")).toBe("https://github.com/owner/assigned.git");
  expect(f.cli(["start", "app", "--repo", "other/repo", "--no-agent"]).code).toBe(1);
});

test("nonempty non-repository directory remains intact", () => {
  const f = fixture(); mkdirSync(f.source, { recursive: true }); writeFileSync(join(f.source, "important.txt"), "preserve");
  expect(f.cli(["start", "app", "--no-agent"]).code).toBe(1);
  expect(readFileSync(join(f.source, "important.txt"), "utf8")).toBe("preserve");
});

test("stop retains source and metadata; active agent blocks rebuild", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const meta = join(f.state, "projects/app/project.json"), before = readFileSync(meta, "utf8");
  expect(f.cli(["stop", "app"]).code).toBe(0);
  expect(f.cli(["rebuild", "app"], { FAKE_RUNNING: "1", FAKE_AGENT_ACTIVE: "1" }).code).toBe(1);
  expect(readFileSync(meta, "utf8")).toBe(before);
  expect(existsSync(join(f.source, "README.md"))).toBe(true);
});

test("dispatch flock stays held across a callback and releases after success or failure", () => {
  const home = mkdtempSync(join(tmpdir(), "dev-dispatch-")); roots.push(home);
  const path = join(home, ".agent-session.lock");
  const probe = () => Bun.spawnSync(["flock", "-n", path, "true"], { stdout: "pipe", stderr: "pipe" }).exitCode;
  withAgentDispatchLock(home, () => expect(probe()).not.toBe(0));
  expect(probe()).toBe(0);
  expect(() => withAgentDispatchLock(home, () => { expect(probe()).not.toBe(0); throw Error("build failed"); })).toThrow("build failed");
  expect(probe()).toBe(0);
  expect(() => withAgentDispatchLock(join(home, "missing"), () => {})).toThrow();
  const link = join(home, "linked-home"); symlinkSync(home, link);
  expect(() => withAgentDispatchLock(link, () => {})).toThrow("shared project home");
  unlinkSync(path); symlinkSync(join(home, "missing-lock"), path);
  expect(() => withAgentDispatchLock(home, () => {})).toThrow();
});

test("an agent admitted during the base build prevents recreation without losing its work", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const path = join(f.state, "projects/app/project.json"), before = readFileSync(path, "utf8");
  const marker = join(f.dir, "late-agent"); f.clear();
  try {
    const result = f.cli(["rebuild", "app"], { FAKE_RUNNING: "1", FAKE_AGENT_DURING_BUILD: marker });
    expect(existsSync(marker)).toBe(true);
    expect(result.code).toBe(1);
    expect(result.err).toContain("Agent active or dispatch lock unavailable");
    expect(f.calls().some(c => c.name === "devcontainer" && c.args[0] === "up")).toBe(false);
    expect(f.calls().some(c => c.args.some((a: string) => a.includes("dev-ui _checkpoint")))).toBe(false);
    expect(readFileSync(path, "utf8")).toBe(before);
  } finally {
    if (existsSync(marker)) process.kill(Number(readFileSync(marker, "utf8")));
  }
});

test("custom build and recreation hold dispatch while the later interactive attach can acquire it", () => {
  const f = fixture();
  const result = f.cli(["start", "app"], { FAKE_CUSTOM_BUILD: "1", FAKE_WORKSPACE_MISSING: "1", FAKE_CHECK_DISPATCH_LOCK: "1" });
  expect(result.code).toBe(0);
  expect(f.calls().filter(c => c.name === "dispatch-lock").map(c => [c.phase, c.held])).toEqual([
    ["build", true], ["up", true], ["agent", false],
  ]);
  const path = join(f.state, "projects/app/project.json"), before = readFileSync(path, "utf8"); f.clear();
  const unknown = f.cli(["rebuild", "app"], { FAKE_RUNNING: "1", FAKE_HOME_UNKNOWN: "/unexpected/home" });
  expect(unknown.code).toBe(1);
  expect(unknown.err).toContain("unknown project home mount");
  expect(f.calls().some(c => c.name === "devcontainer" && c.args[0] === "up")).toBe(false);
  expect(readFileSync(path, "utf8")).toBe(before);
});

test("custom builds use a project image and failed rebuild preserves metadata and source", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const meta = join(f.state, "projects/app/project.json"), before = readFileSync(meta, "utf8");
  const saved = JSON.parse(before);
  expect(saved.image).toContain(saved.composeProject);
  expect(saved.image).not.toBe(saved.baseImage);
  mkdirSync(join(f.source, ".dev")); writeFileSync(join(f.source, ".dev/compose.yaml"), "services: {}\n");
  f.clear();
  expect(f.cli(["start", "app", "--no-agent"], { FAKE_WORKSPACE_MISSING: "1", FAKE_CUSTOM_BUILD: "1" }).code).toBe(0);
  expect(f.calls().some(c => c.name === "devcontainer" && c.args[0] === "build")).toBe(true);
  expect(f.calls().some(c => c.name === "docker" && c.args.slice(0, 2).join(" ") === "image tag")).toBe(false);
  const beforeFailure = readFileSync(meta, "utf8");
  expect(f.cli(["rebuild", "app"], { FAKE_UP_FAIL: "1", FAKE_CUSTOM_BUILD: "1" }).code).toBe(1);
  expect(readFileSync(meta, "utf8")).toBe(beforeFailure);
  expect(existsSync(join(f.source, "README.md"))).toBe(true);
});

test("native lifecycle adopts legacy state once and reuses unchanged running or stopped boxes", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const path = join(f.state, "projects/app/project.json"), meta = JSON.parse(readFileSync(path, "utf8"));
  delete meta.configHash; writeFileSync(path, JSON.stringify(meta)); f.clear();
  expect(f.cli(["start", "app", "--no-agent"], { FAKE_RUNNING: "1" }).code).toBe(0);
  expect(f.calls().find(c => c.name === "devcontainer" && c.args[0] === "up").args).toContain("--remove-existing-container");
  for (const extra of [{}, { FAKE_RUNNING: "1" }]) {
    f.clear(); expect(f.cli(["start", "app", "--no-agent"], extra).code).toBe(0);
    const up = f.calls().find(c => c.name === "devcontainer" && c.args[0] === "up");
    if (extra.FAKE_RUNNING) expect(up).toBeUndefined();
    else { expect(up.args).not.toContain("--remove-existing-container"); expect(up.args).toContain(`com.docker.compose.project=${meta.composeProject}`); }
    expect(f.calls().some(c => c.args.some(value => value.includes("dev-ui _checkpoint")))).toBe(false);
    expect(f.calls().filter(c => c.name === "docker" && c.args.includes("exec")).every(c => c.args.includes("flock"))).toBe(true);
    expect(f.calls().some(c => c.name === "docker" && c.args.includes("up"))).toBe(false);
    expect(f.calls().some(c => ["curl", "sh"].includes(c.name))).toBe(false);
  }
  const config = JSON.parse(readFileSync(join(f.state, "projects/app/devcontainer.json"), "utf8"));
  expect(config).toMatchObject({ remoteUser: "node", updateRemoteUserUID: false, overrideCommand: false, userEnvProbe: "none", shutdownAction: "none" });
  expect(config.runServices).toBeUndefined(); // Keep Compose's full project/profile behavior.
  expect(statSync(join(f.state, "projects/app/compose.json")).mode & 0o777).toBe(0o600);
});

test("snapshot/config changes request native recreation and preserve metadata on unhealthy startup", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const path = join(f.state, "projects/app/project.json"), before = readFileSync(path, "utf8");
  const snapshot = join(f.dir, "snapshot"); mkdirSync(snapshot); f.clear();
  expect(f.cli(["start", "app", "--no-agent"], { DEV_AGENT_CONFIG: snapshot, FAKE_HEALTH_FAIL: "1" }).code).toBe(1);
  expect(f.calls().find(c => c.name === "devcontainer" && c.args[0] === "up").args).toContain("--remove-existing-container");
  expect(readFileSync(path, "utf8")).toBe(before);
  expect(f.calls().some(c => c.args.includes("dev-agent"))).toBe(false);
  const fresh = fixture();
  expect(fresh.cli(["start", "app"], { FAKE_MISSING: "1", FAKE_HEALTH_FAIL: "1" }).code).toBe(1);
  const identity = JSON.parse(readFileSync(join(fresh.state, "projects/app/project.json"), "utf8"));
  expect(identity.privateDefault).toBe(true); expect(identity.configHash).toBeUndefined();
});

test("native project builds stay out of the runtime Compose file and normal restarts", () => {
  const f = fixture();
  expect(f.cli(["start", "app", "--no-agent"], { FAKE_CUSTOM_BUILD: "1", FAKE_WORKSPACE_MISSING: "1" }).code).toBe(0);
  const config = JSON.parse(readFileSync(join(f.state, "projects/app/compose.json"), "utf8"));
  expect(config.services.dev.build).toBeUndefined();
  f.clear(); expect(f.cli(["start", "app", "--no-agent"], { FAKE_CUSTOM_BUILD: "1" }).code).toBe(0);
  expect(f.calls().some(c => c.name === "devcontainer" && c.args[0] === "build")).toBe(false);
});

test("host CLI uses its official installer only during explicit setup", () => {
  const f = fixture();
  expect(f.cli(["start", "app", "--no-agent"], { FAKE_CLI_VERSION: "0.88.0" }).err).toContain("run dev setup");
  expect(existsSync(f.source)).toBe(false);
  f.clear();
  expect(f.cli(["setup"], { HOME: f.dir, FAKE_CLI_VERSION: "0.88.0" }).code).toBe(0);
  expect(f.calls().find(c => c.name === "curl").args).toContain("https://raw.githubusercontent.com/devcontainers/cli/main/scripts/install.sh");
  expect(f.calls().find(c => c.name === "sh").args.slice(1)).toEqual(["--version", "0.89.0", "--node-version", "24", "--prefix", join(f.dir, ".local/share/devcontainers")]);
  f.clear(); expect(f.cli(["setup"], { HOME: f.dir }).code).toBe(0);
  expect(f.calls().some(c => c.name === "curl" || c.name === "sh")).toBe(false);
});

test("host CLI lock updates do not invalidate the shared development image", () => {
  const f = fixture(), snapshot = join(f.dir, "snapshot"); mkdirSync(snapshot);
  const lock = join(snapshot, "tools.lock.json");
  writeFileSync(lock, JSON.stringify({ devcontainerCli: { version: "0.89.0" } }));
  expect(f.cli(["build"], { DEV_AGENT_CONFIG: snapshot }).code).toBe(0);
  const image = f.calls().find(c => c.name === "docker" && c.args[0] === "build").args[2];
  f.clear(); writeFileSync(lock, JSON.stringify({ devcontainerCli: { version: "0.90.0" } }));
  expect(f.cli(["build"], { DEV_AGENT_CONFIG: snapshot }).code).toBe(0);
  expect(f.calls().find(c => c.name === "docker" && c.args[0] === "build").args[2]).toBe(image);
  expect(f.cli(["start", "app", "--no-agent"], { DEV_AGENT_CONFIG: snapshot }).err).toContain("0.90.0 required");
});

test("runtime refresh reuses the locked CLI installer and rejects an old Node runtime", () => {
  const f = fixture();
  expect(f.cli(["setup", "--refresh-runtime"], { HOME: f.dir }).code).toBe(0);
  expect(f.calls().find(c => c.name === "sh").args).toContain("0.89.0");
  expect(f.calls().some(c => c.name === "node" && c.args[0] === "--version")).toBe(true);
  f.clear();
  expect(f.cli(["setup", "--refresh-runtime"], { HOME: f.dir, FAKE_NODE_VERSION: "v24.21.0" }).err).toContain("Patched Node.js 24 required");
  expect(f.cli(["setup", "--update"]).err).toContain("Usage: dev setup");
});

test("image builds pull the pinned base and refresh apt package layers weekly", () => {
  const f = fixture();
  expect(f.cli(["build"], { FAKE_CHECK_BUILD_LOCK: "1" }).code).toBe(0);
  expect(Bun.spawnSync(["flock", "-n", f.env.DEV_DEPLOYMENT_LOCK, "true"]).exitCode).toBe(0);
  const build = f.calls().find(c => c.name === "docker" && c.args[0] === "build").args;
  expect(build).toContain("--pull");
  expect(build.some((arg: string) => /^APT_REFRESH=\d+$/.test(arg))).toBe(true);
});

test("warm starts reconcile sidecars without replacing the running development container", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"], { FAKE_SIDECAR: "1" }).code).toBe(0);
  f.clear(); expect(f.cli(["start", "app", "--no-agent"], { FAKE_SIDECAR: "1", FAKE_RUNNING: "1" }).code).toBe(0);
  expect(f.calls().some(c => c.name === "devcontainer" && c.args[0] === "up")).toBe(false);
  const sidecars = f.calls().find(c => c.name === "docker" && c.args.includes("up"));
  expect(sidecars.args.at(-1)).toBe("db");
  expect(sidecars.args).toContain("--no-deps"); expect(sidecars.args).toContain("--wait");
});

test("concurrent startup fails promptly while process-owned lock is held", async () => {
  const f = fixture(); const lock = join(f.state, "projects/app/startup.lock"), marker = join(f.dir, "locked");
  mkdirSync(join(f.state, "projects/app"), { recursive: true });
  const child = Bun.spawn(["flock", "--no-fork", lock, process.execPath, "-e", `await Bun.write(${JSON.stringify(marker)}, "ready"); await Bun.sleep(10000);`], { env: f.env, stdout: "ignore", stderr: "pipe" });
  try {
    for (let i = 0; i < 100 && !existsSync(marker); i++) await Bun.sleep(10);
    expect(existsSync(marker)).toBe(true);
    const result = f.cli(["start", "app", "--no-agent"]);
    expect(result.code).toBe(1);
    expect(result.err).toContain("preparation already in progress");
    expect(existsSync(f.source)).toBe(false);
  } finally { child.kill(); await child.exited; }
});

test("new project admission waits for core deployment before creating a container", async () => {
  const f = fixture(), marker = join(f.dir, "deployment-held");
  const holder = Bun.spawn(["flock", "--no-fork", f.env.DEV_DEPLOYMENT_LOCK, process.execPath, "-e", `await Bun.write(${JSON.stringify(marker)}, "ready"); await Bun.sleep(10000);`], { stdout: "ignore", stderr: "pipe" });
  let start: ReturnType<typeof Bun.spawn> | undefined;
  try {
    for (let i = 0; i < 100 && !existsSync(marker); i++) await Bun.sleep(10);
    expect(existsSync(marker)).toBe(true);
    start = Bun.spawn([process.execPath, script, "start", "app", "--no-agent"], { env: f.env, stdout: "pipe", stderr: "pipe" });
    const startup = join(f.state, "projects/app/startup.lock");
    for (let i = 0; i < 100 && !existsSync(startup); i++) await Bun.sleep(10);
    expect(existsSync(startup)).toBe(true);
    await Bun.sleep(50);
    expect(existsSync(f.source)).toBe(false);
    expect(f.calls().some(c => c.name === "docker" || c.name === "gh")).toBe(false);
    holder.kill(); await holder.exited;
    expect(await start.exited).toBe(0);
    expect(f.calls().some(c => c.name === "devcontainer" && c.args[0] === "up")).toBe(true);
  } finally {
    holder.kill(); await holder.exited;
    if (start) { start.kill(); await start.exited; }
  }
});

test("manual stop waits for deployment so fleet recovery cannot undo its intent", async () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0); f.clear();
  const marker = join(f.dir, "deployment-held");
  const holder = Bun.spawn(["flock", "--no-fork", f.env.DEV_DEPLOYMENT_LOCK, process.execPath, "-e", `await Bun.write(${JSON.stringify(marker)}, "ready"); await Bun.sleep(10000);`], { stdout: "ignore", stderr: "pipe" });
  let stop: ReturnType<typeof Bun.spawn> | undefined;
  try {
    for (let i = 0; i < 100 && !existsSync(marker); i++) await Bun.sleep(10);
    expect(existsSync(marker)).toBe(true);
    stop = Bun.spawn([process.execPath, script, "stop", "app"], { env: { ...f.env, FAKE_RUNNING: "1" }, stdout: "pipe", stderr: "pipe" });
    let waiting = false;
    for (let i = 0; i < 100 && !waiting; i++) {
      waiting = readFileSync(`/proc/${stop.pid}/task/${stop.pid}/children`, "utf8").trim().length > 0;
      if (!waiting) await Bun.sleep(10);
    }
    expect(waiting).toBe(true);
    expect(f.calls().some(c => c.name === "docker" || c.name === "devcontainer")).toBe(false);
    holder.kill(); await holder.exited;
    expect(await stop.exited).toBe(0);
    expect(f.calls().some(c => c.name === "docker" && c.args.at(-1) === "stop")).toBe(true);
  } finally {
    holder.kill(); await holder.exited;
    if (stop) { stop.kill(); await stop.exited; }
  }
});

test("failed initial push preserves local scaffold commit for recovery", () => {
  const f = fixture(); const r = f.cli(["start", "app", "--no-agent"], { FAKE_PUSH_FAIL: "1" });
  expect(r.code).toBe(1); expect(f.git("log", "-1", "--format=%s")).toBe("Initialize private development workspace");
  expect(existsSync(join(f.source, "README.md"))).toBe(true);
  f.clear();
  expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  expect(f.calls().some(c => c.name === "git" && c.args.includes("push") && c.args.includes("main"))).toBe(true);
  expect(existsSync(join(f.state, "projects/app/bootstrap-push"))).toBe(false);
});

test("dev-finish blocks failed checks, wrong branches, secrets, and unrelated staged work", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const finish = (check: string, paths: string[]) => Bun.spawnSync(["bash", join(import.meta.dir, "dev-finish.sh"), "Task change", "--check", check, "--", ...paths], { cwd: f.source, env: f.env, stdout: "pipe", stderr: "pipe" });
  const initial = f.git("rev-parse", "HEAD");
  writeFileSync(join(f.source, "task.txt"), "work");
  expect(finish("exit 1", ["task.txt"]).exitCode).toBe(1);
  expect(f.git("diff", "--cached", "--name-only")).toBe("");
  expect(finish("true", [":(invalid)task.txt"]).exitCode).not.toBe(0);
  f.git("add", "task.txt"); expect(finish("true", ["task.txt"]).exitCode).toBe(1); f.git("reset", "--", "task.txt");
  writeFileSync(join(f.source, "auth.json"), "secret");
  expect(finish("true", ["auth.json"]).exitCode).toBe(1); f.git("reset", "--", "auth.json");
  f.git("switch", "main"); expect(finish("true", ["task.txt"]).exitCode).toBe(1);
  expect(f.git("rev-parse", "HEAD")).toBe(initial);
});

test("dev-finish commits selected files only and preserves commit after failed push", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  writeFileSync(join(f.source, "task.txt"), "work"); writeFileSync(join(f.source, "unrelated.txt"), "not mine");
  const r = Bun.spawnSync(["bash", join(import.meta.dir, "dev-finish.sh"), "Task change", "--check", "test -s task.txt", "--", "task.txt"], { cwd: f.source, env: { ...f.env, FAKE_PUSH_FAIL: "1" }, stdout: "pipe", stderr: "pipe" });
  expect(r.exitCode).toBe(1);
  expect(f.git("log", "-1", "--format=%s")).toBe("Task change");
  expect(f.git("ls-files")).toContain("task.txt");
  expect(f.git("ls-files")).not.toContain("unrelated.txt");
});

test("active agent prevents a second start from changing its branch", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const branch = f.git("branch", "--show-current");
  const r = f.cli(["start", "app", "--task", "overlap", "--no-agent"], { FAKE_RUNNING: "1", FAKE_AGENT_ACTIVE: "1" });
  expect(r.code).toBe(1);
  expect(f.git("branch", "--show-current")).toBe(branch);
});

test("launcher holds the project lock through the agent session while shell and stop remain available", async () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const marker = join(f.dir, "agent-started"), branch = f.git("branch", "--show-current");
  const child = Bun.spawn([process.execPath, script, "start", "app"], { env: { ...f.env, FAKE_AGENT_STARTED: marker }, stdout: "pipe", stderr: "pipe" });
  for (let i = 0; i < 100 && !existsSync(marker); i++) await Bun.sleep(10);
  expect(existsSync(marker)).toBe(true);
  expect(Bun.spawnSync(["flock", "-n", f.env.DEV_DEPLOYMENT_LOCK, "true"]).exitCode).toBe(0);
  expect(f.cli(["start", "app", "--task", "overlap", "--no-agent"]).code).toBe(1);
  expect(f.cli(["rebuild", "app"]).code).toBe(1);
  expect(f.cli(["shell", "app", "--", "true"]).code).toBe(0);
  expect(f.cli(["view", "app"], { FAKE_RUNNING: "1" }).code).toBe(0);
  expect(f.cli(["stop", "app"]).code).toBe(0);
  expect(f.git("branch", "--show-current")).toBe(branch);
  expect(await child.exited).toBe(0);
});

test("dev-finish retries a pending push even when files are unchanged", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  writeFileSync(join(f.source, "task.txt"), "work");
  const args = ["bash", join(import.meta.dir, "dev-finish.sh"), "Task change", "--check", "test -s task.txt", "--", "task.txt"];
  expect(Bun.spawnSync(args, { cwd: f.source, env: { ...f.env, FAKE_PUSH_FAIL: "1" } }).exitCode).toBe(1);
  const commit = f.git("rev-parse", "HEAD"); f.clear();
  expect(Bun.spawnSync(args, { cwd: f.source, env: f.env }).exitCode).toBe(0);
  expect(f.git("rev-parse", "HEAD")).toBe(commit);
  expect(f.calls().some(c => c.name === "git" && c.args.includes("push"))).toBe(true);
});

test("dev-finish aborts if its check changes the task branch", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const initial = f.git("rev-parse", "HEAD");
  writeFileSync(join(f.source, "task.txt"), "work");
  const r = Bun.spawnSync(["bash", join(import.meta.dir, "dev-finish.sh"), "Task change", "--check", "git switch main", "--", "task.txt"], { cwd: f.source, env: f.env });
  expect(r.exitCode).toBe(1);
  expect(f.git("rev-parse", "main")).toBe(initial);
  expect(f.git("diff", "--cached", "--name-only")).toBe("");
});

test("dev-finish retries a deleted path alongside unchanged paths without including unrelated files", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  writeFileSync(join(f.source, "old task.txt"), "remove this");
  f.git("add", "--", "old task.txt"); f.git("commit", "-m", "Add removable file");
  rmSync(join(f.source, "old task.txt"));
  const args = ["bash", join(import.meta.dir, "dev-finish.sh"), "Remove old task", "--check", "test ! -e 'old task.txt'", "--", "old task.txt", "README.md"];
  expect(Bun.spawnSync(args, { cwd: f.source, env: { ...f.env, FAKE_PUSH_FAIL: "1" } }).exitCode).toBe(1);
  const commit = f.git("rev-parse", "HEAD");
  expect(f.git("ls-files")).not.toContain("old task.txt");
  writeFileSync(join(f.source, "unrelated.txt"), "not mine"); f.clear();
  expect(Bun.spawnSync(args, { cwd: f.source, env: f.env }).exitCode).toBe(0);
  expect(f.git("rev-parse", "HEAD")).toBe(commit);
  expect(f.git("ls-files")).not.toContain("unrelated.txt");
  expect(f.calls().some(c => c.name === "git" && c.args.includes("push"))).toBe(true);
});

test("dev-finish rejects and preserves files staged by the check", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const initial = f.git("rev-parse", "HEAD");
  writeFileSync(join(f.source, "task.txt"), "my task");
  writeFileSync(join(f.source, "unrelated.txt"), "not mine"); f.clear();
  const r = Bun.spawnSync(["bash", join(import.meta.dir, "dev-finish.sh"), "Task change", "--check", "git add unrelated.txt", "--", "task.txt"], { cwd: f.source, env: f.env });
  expect(r.exitCode).toBe(1);
  expect(f.git("rev-parse", "HEAD")).toBe(initial);
  expect(f.git("diff", "--cached", "--name-only")).toBe("unrelated.txt");
  expect(f.calls().some(c => c.name === "git" && c.args.includes("push"))).toBe(false);
});
