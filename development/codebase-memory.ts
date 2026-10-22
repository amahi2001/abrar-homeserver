#!/usr/bin/env bun
import { existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, writeFileSync, renameSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";

const binary = "/opt/codebase-memory-mcp/codebase-memory-mcp";
// Source checkouts, including nested repos/worktrees; never index tool/dependency caches.
const excluded = new Set([".git", "node_modules", "target", "dist", ".venv", "venv", "vendor", ".cache", ".local", ".bun", ".npm", ".cargo", ".rustup", ".agents", ".claude", ".codex", ".config"]);

export function repositories(roots: string[]): string[] {
  const seen = new Set<string>(), repos = new Set<string>();
  function visit(path: string) {
    if (!existsSync(path)) return;
    const canonical = realpathSync(path);
    if (seen.has(canonical)) return;
    seen.add(canonical);
    if (existsSync(join(canonical, ".git"))) {
      const git = Bun.spawnSync(["git", "-C", canonical, "rev-parse", "--show-toplevel"], { stdout: "pipe", stderr: "pipe" });
      if (git.exitCode !== 0) throw Error(`Cannot resolve Git repository ${canonical}: ${git.stderr.toString().trim()}`);
      if (realpathSync(git.stdout.toString().trim()) === canonical) repos.add(canonical);
    }
    for (const entry of readdirSync(canonical, { withFileTypes: true })) {
      if (entry.isDirectory() && !excluded.has(entry.name)) visit(join(canonical, entry.name));
    }
  }
  for (const root of roots) visit(root);
  return [...repos].sort();
}

function run(args: string[]): string {
  const result = Bun.spawnSync([binary, ...args], { stdout: "pipe", stderr: "pipe" });
  if (result.exitCode !== 0) throw Error(`Codebase Memory ${args[0]} failed (${result.exitCode}): ${result.stderr.toString().trim()} ${result.stdout.toString().trim()}`);
  return result.stdout.toString().trim();
}

export function requireIndexed(output: string, repo: string): void {
  let envelope: any, result: any;
  try {
    envelope = JSON.parse(output);
    result = envelope.structuredContent ?? JSON.parse(envelope.content?.find((item: any) => item.type === "text")?.text ?? "null");
  } catch {
    throw Error(`Codebase Memory returned an invalid index result for ${repo}`);
  }
  if (envelope.isError === true || result?.status !== "indexed") {
    throw Error(`Codebase Memory did not finish indexing ${repo}: ${JSON.stringify(result ?? envelope).slice(0, 4096)}`);
  }
}

export function main(args = process.argv.slice(2)) {
  const home = homedir();
  // Upstream requires private, user-owned cache parents. Graphs persist in this
  // project's home; coordination sockets stay in container-local /tmp.
  mkdirSync(join(home, ".cache/codebase-memory-mcp"), { recursive: true, mode: 0o700 });
  if (args.length !== 1 || !["setup", "index"].includes(args[0])) throw Error("Usage: dev-memory setup|index");
  if (args[0] === "setup") {
    const codex = process.env.CODEX_HOME!, claude = process.env.CLAUDE_CONFIG_DIR!;
    const version = run(["--version"]), stamp = join(codex, ".dev-codebase-memory-version");
    const required = [join(codex, "skills/codebase-memory/SKILL.md"), join(codex, "config.toml"), join(claude, ".claude.json"), join(claude, "skills/codebase-memory/SKILL.md")];
    if (!existsSync(stamp) || readFileSync(stamp, "utf8").trim() !== version || required.some(p => !existsSync(p))) {
      // Called under entrypoint's cross-container account-config flock.
      console.log(run(["install", "-y", "--force", "--skip-binary", `--dir=${dirname(binary)}`, "--clients=claude,codex"]));
      writeFileSync(`${stamp}.tmp`, `${version}\n`, { mode: 0o600 });
      renameSync(`${stamp}.tmp`, stamp);
    }
    // Native settings, rather than a custom MCP wrapper or fixed shared cache.
    for (const [key, value] of [["auto_index", "true"], ["auto_watch", "true"], ["ui_enabled", "false"]]) run(["config", "set", key, value]);
    return;
  }
  for (const repo of repositories(["/workspace", home, resolve(process.cwd())])) {
    const started = performance.now();
    requireIndexed(run(["cli", "--json", "--quiet", "index_repository", "--repo-path", repo]), repo);
    console.log(`Codebase Memory indexed ${repo} (${((performance.now() - started) / 1000).toFixed(2)}s)`);
  }
}

if (import.meta.main) {
  try { main(); } catch (error) { console.error((error as Error).message); process.exitCode = 1; }
}
