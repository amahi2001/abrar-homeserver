import { afterEach, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { repositories, requireIndexed } from "./codebase-memory";

const roots: string[] = [];
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });

function fixture() {
  const root = mkdtempSync(join(tmpdir(), "dev-memory-test-"));
  roots.push(root);
  function git(path: string, ...args: string[]) {
    const result = Bun.spawnSync(["git", "-C", path, ...args], {
      env: {
        ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null",
        GIT_AUTHOR_NAME: "Test", GIT_AUTHOR_EMAIL: "test@example.invalid",
        GIT_COMMITTER_NAME: "Test", GIT_COMMITTER_EMAIL: "test@example.invalid",
      },
      stdout: "pipe", stderr: "pipe",
    });
    if (result.exitCode !== 0) throw Error(result.stderr.toString());
  }
  function repo(relative: string) {
    const path = join(root, relative);
    mkdirSync(path, { recursive: true });
    git(path, "init", "--quiet", "--initial-branch=main", "--template=");
    return realpathSync(path);
  }
  return { root: realpathSync(root), git, repo };
}

test("discovers nested repositories and linked Git worktrees without indexing their parent directory", () => {
  const f = fixture(), outer = f.repo("workspace/outer"), nested = f.repo("workspace/outer/packages/nested");
  f.git(outer, "commit", "--quiet", "--allow-empty", "-m", "fixture");
  const worktree = join(f.root, "workspace/linked");
  f.git(outer, "worktree", "add", "--quiet", "-b", "linked", worktree);
  expect(repositories([join(f.root, "workspace")])).toEqual([outer, nested, worktree].sort());
});

test("prunes dependency, build and agent caches but keeps source repositories", () => {
  const f = fixture(), source = f.repo("workspace/source");
  for (const dir of ["node_modules", "target", "dist", ".venv", "vendor", ".cache", ".local", ".bun", ".npm", ".cargo", ".rustup", ".agents", ".claude", ".codex", ".config"]) {
    f.repo(`workspace/${dir}/downloaded-repo`);
  }
  expect(repositories([join(f.root, "workspace")])).toEqual([source]);
});

test("deduplicates canonical root aliases, ignores inner directory symlinks and tolerates missing roots", () => {
  const f = fixture(), source = f.repo("workspace/source"), outside = f.repo("outside/source");
  symlinkSync(join(f.root, "workspace"), join(f.root, "workspace-alias"));
  symlinkSync(outside, join(source, "outside-link"));
  symlinkSync(join(f.root, "workspace"), join(source, "cycle"));
  expect(repositories([join(f.root, "missing"), join(f.root, "workspace"), join(f.root, "workspace-alias"), source])).toEqual([source]);
  // An explicitly supplied symlink root is still a requested checkout.
  expect(repositories([join(source, "outside-link"), outside])).toEqual([outside]);
});

test("refuses invalid Git metadata instead of silently reporting that every repository was indexed", () => {
  const f = fixture(), broken = join(f.root, "workspace/broken");
  mkdirSync(broken, { recursive: true });
  writeFileSync(join(broken, ".git"), "gitdir: /does-not-exist/dev-memory-test\n");
  expect(() => repositories([join(f.root, "workspace")])).toThrow("Cannot resolve Git repository");
});

test("accepts completed native index envelopes and refuses degraded, failed or invalid results", () => {
  const result = { status: "indexed", nodes: 10, edges: 12 };
  expect(() => requireIndexed(JSON.stringify({ structuredContent: result, isError: false }), "/workspace/source")).not.toThrow();
  expect(() => requireIndexed(JSON.stringify({ content: [{ type: "text", text: JSON.stringify(result) }], isError: false }), "/workspace/source")).not.toThrow();
  const degraded = { status: "degraded", dump_verify: { expected_nodes: 1000, actual_nodes: 100 } };
  expect(() => requireIndexed(JSON.stringify({ structuredContent: degraded, isError: false }), "/workspace/source")).toThrow('"actual_nodes":100');
  expect(() => requireIndexed(JSON.stringify({ structuredContent: result, isError: true }), "/workspace/source")).toThrow("did not finish indexing");
  for (const output of ["", "not-json", "null", "{}", '{"structuredContent":{"status":"up_to_date"}}', '{"content":[{"type":"text","text":"not-json"}]}']) {
    expect(() => requireIndexed(output, "/workspace/source")).toThrow("Codebase Memory");
  }
});
