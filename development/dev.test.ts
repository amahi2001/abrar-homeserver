import { test, expect, afterEach } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, statSync, copyFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { projectName, repository, githubRemote } from "./dev";

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
  for (const file of ["Dockerfile", "entrypoint.sh", "agent-session.sh", "dev-finish.sh", "gh.sh", "instructions.md"]) copyFileSync(join(import.meta.dir, file), join(assets, file));
  writeFileSync(join(assets, "skills/ponytail/SKILL.md"), "test fixture skill");
  mkdirSync(join(dir, "host-codex"));
  writeFileSync(join(dir, "host-codex/auth.json"), '{"test":"fake-codex-credential"}');
  const mock = `#!${process.execPath}
import {appendFileSync,existsSync,writeFileSync} from 'node:fs';
const name=process.argv[1].split('/').pop(), a=process.argv.slice(2);
appendFileSync(process.env.FAKE_LOG,JSON.stringify({name,args:a})+'\\n');
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
 if(a.includes('dev-agent')&&process.env.FAKE_AGENT_STARTED) {writeFileSync(process.env.FAKE_AGENT_STARTED,'ready');await Bun.sleep(600);}
 if(a[0]==='info')console.log('29.8.1');
 if(a.includes('config'))console.log(JSON.stringify({services:{dev:{build:process.env.FAKE_CUSTOM_BUILD?{}:undefined}}}));
 if(a.includes('inspect')&&process.env.FAKE_IMAGE_MISSING)fail('image absent');
 if(a.includes('inspect')&&process.env.FAKE_WORKSPACE_MISSING&&a.at(-1).includes('-dev-'))fail('workspace image absent');
 if(a.includes('up')&&process.env.FAKE_UP_FAIL)fail('container unhealthy');
 if(a.includes('ps')&&a.includes('-q')&&process.env.FAKE_RUNNING)console.log('container-id');
 if(a.includes('flock')&&process.env.FAKE_AGENT_ACTIVE)fail('locked');
 process.exit(0);
}
fail('unexpected mock');
`;
  for (const name of ["git", "gh", "docker"]) writeFileSync(join(bin, name), mock, { mode: 0o755 });
  const env = {
    ...process.env, PATH: `${bin}:${process.env.PATH}`, REAL_GIT: realGit,
    DEV_ROOT: join(dir, "projects"), DEV_STATE_ROOT: state, CODEX_HOME: join(dir, "host-codex"),
    DEV_ASSETS: assets,
    DEV_LOCK_HELD: "", FAKE_LOG: join(dir, "calls.jsonl"), FAKE_CREATED: join(dir, "created"),
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
    calls() { return existsSync(env.FAKE_LOG) ? readFileSync(env.FAKE_LOG, "utf8").trim().split("\n").map(line => JSON.parse(line)) : []; },
    clear() { writeFileSync(env.FAKE_LOG, ""); },
  };
}

test("rejects path traversal, URLs, shell strings, and malformed project/repository names", () => {
  for (const bad of ["../escape", "a/b", "-flag", "My App", "x;touch y", "a\nb"]) expect(() => projectName(bad)).toThrow();
  for (const bad of ["https://github.com/a/b", "../repo", "a/..", "a/b/c", "a/b;foo"]) expect(() => repository(bad)).toThrow();
  expect(githubRemote("git@github.com:owner/repo.git")).toBe("owner/repo");
  expect(githubRemote("https://github.com/owner/repo.git")).toBe("owner/repo");
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
  expect(r.out + r.err + JSON.stringify(f.calls())).not.toContain("fake-github-credential");
  expect(f.calls().find(c => c.name === "git" && c.args.includes("push"))?.args).toContain("main");
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

test("custom builds use a project image and failed rebuild preserves metadata and source", () => {
  const f = fixture(); expect(f.cli(["start", "app", "--no-agent"]).code).toBe(0);
  const meta = join(f.state, "projects/app/project.json"), before = readFileSync(meta, "utf8");
  const saved = JSON.parse(before);
  expect(saved.image).toContain(saved.composeProject);
  expect(saved.image).not.toBe(saved.baseImage);
  mkdirSync(join(f.source, ".dev")); writeFileSync(join(f.source, ".dev/compose.yaml"), "services: {}\n");
  f.clear();
  expect(f.cli(["start", "app", "--no-agent"], { FAKE_WORKSPACE_MISSING: "1", FAKE_CUSTOM_BUILD: "1" }).code).toBe(0);
  expect(f.calls().some(c => c.name === "docker" && c.args.slice(-2).join(" ") === "build dev")).toBe(true);
  expect(f.calls().some(c => c.name === "docker" && c.args.slice(0, 2).join(" ") === "image tag")).toBe(false);
  expect(f.cli(["rebuild", "app"], { FAKE_UP_FAIL: "1", FAKE_CUSTOM_BUILD: "1" }).code).toBe(1);
  expect(readFileSync(meta, "utf8")).toBe(before);
  expect(existsSync(join(f.source, "README.md"))).toBe(true);
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
  expect(f.cli(["start", "app", "--task", "overlap", "--no-agent"]).code).toBe(1);
  expect(f.cli(["rebuild", "app"]).code).toBe(1);
  expect(f.cli(["shell", "app", "--", "true"]).code).toBe(0);
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
