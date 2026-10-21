#!/usr/bin/env bun
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync, renameSync, chmodSync, copyFileSync, readdirSync, lstatSync, unlinkSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve, dirname } from "node:path";

const help = `Usage:
  dev start PROJECT [--agent codex|claude] [--repo OWNER/REPO] [--task NAME] [--no-agent] [-- agent arguments]
  dev shell PROJECT [-- command arguments]
  dev stop PROJECT
  dev list
  dev build
  dev rebuild PROJECT

Projects: ~/projects/PROJECT. New unassigned projects get private GitHub repos.
Agent defaults to Codex. Use --no-agent to prepare without opening a session.
Project overrides: .dev/compose.yaml (Compose override; build with dev rebuild).
Finish inside container: dev-finish "message" --check "test command" -- file ...
`;

export function projectName(value: string): string {
  if (!/^[a-z0-9][a-z0-9-]{0,47}$/.test(value)) throw Error("Project must be 1–48 lowercase letters, digits, or hyphens, starting with a letter or digit.");
  return value;
}
export function repository(value: string): string {
  if (!/^[\w.-]+\/[\w.-]+$/.test(value) || value.split("/").some(p => /^\.+$/.test(p))) throw Error("Repository must be OWNER/REPO, not a URL or filesystem path.");
  return value;
}
export function githubRemote(value: string): string {
  const match = /^(?:https:\/\/github\.com\/|git@github\.com:)([\w.-]+\/[\w.-]+?)(?:\.git)?$/.exec(value);
  if (!match) throw Error("Existing origin is not a supported GitHub URL; specify and reconcile its repository explicitly.");
  return repository(match[1]);
}

function run(args: string[], options: { cwd?: string; env?: Record<string, string>; inherit?: boolean; allowFailure?: boolean } = {}) {
  const result = Bun.spawnSync(args, {
    cwd: options.cwd,
    env: { ...process.env, ...options.env, GIT_TERMINAL_PROMPT: "0" },
    stdin: options.inherit ? "inherit" : "ignore",
    stdout: options.inherit ? "inherit" : "pipe",
    stderr: options.inherit ? "inherit" : "pipe",
  });
  const stdout = result.stdout?.toString().trim() ?? "";
  const stderr = result.stderr?.toString().trim() ?? "";
  if (result.exitCode !== 0 && !options.allowFailure) throw Error(`${args[0]} failed (${result.exitCode}): ${stderr || "see command output"}`);
  return { code: result.exitCode, stdout, stderr };
}

function atomic(path: string, data: string, mode = 0o600) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const temp = `${path}.${process.pid}.tmp`;
  writeFileSync(temp, data, { mode });
  chmodSync(temp, mode);
  renameSync(temp, path);
}

type Metadata = { repo: string; privateDefault: boolean; source: string; composeProject: string; image: string; baseImage?: string };

export function main(argv = process.argv.slice(2)) {
  const [command, rawProject, ...rest] = argv;
  if (!command || ["help", "--help", "-h"].includes(command)) { console.log(help); return; }
  const assets = process.env.DEV_ASSETS || import.meta.dir;
  const root = resolve(process.env.DEV_ROOT || join(homedir(), "projects"));
  const stateRoot = resolve(process.env.DEV_STATE_ROOT || join(homedir(), ".local/state/dev"));
  const accounts = join(stateRoot, "accounts");
  const credentials = join(stateRoot, "credentials");
  const configExport = resolve(process.env.DEV_AGENT_CONFIG || join(homedir(), ".local/share/agent-config/export"));
  if (process.env.DEV_AGENT_CONFIG && !existsSync(configExport)) throw Error(`Agent config snapshot not found: ${configExport}`);
  const emptyConfig = join(stateRoot, "agent-config-empty");
  const toolsLock = join(configExport, "tools.lock.json");
  const rtk = existsSync(toolsLock) ? JSON.parse(readFileSync(toolsLock, "utf8")).rtk : undefined;
  const personalPackage = join(configExport, "package.json");
  const skillsVersion = existsSync(personalPackage) ? JSON.parse(readFileSync(personalPackage, "utf8")).dependencies.skills : undefined;
  const fingerprint = createHash("sha256");
  if (rtk) fingerprint.update(JSON.stringify(rtk));
  if (skillsVersion) fingerprint.update(skillsVersion);
  function hashFiles(dir: string) {
    for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) hashFiles(path);
      else if (entry.isFile()) { fingerprint.update(path.slice(assets.length)); fingerprint.update(readFileSync(path)); }
    }
  }
  for (const file of ["Dockerfile", "entrypoint.sh", "agent-session.sh", "dev-finish.sh", "gh.sh", "instructions.md"]) fingerprint.update(readFileSync(join(assets, file)));
  if (existsSync(join(assets, "skills"))) hashFiles(join(assets, "skills"));
  const image = `buildfleet-dev:${fingerprint.digest("hex").slice(0, 16)}`;

  function build() {
    const args = ["docker", "build", "--tag", image];
    if (rtk) args.push("--build-arg", `RTK_VERSION=v${rtk.version.replace(/^v/, "")}`, "--build-arg", `RTK_INSTALLER=${rtk.installer}`, "--build-arg", `RTK_INSTALLER_SHA256=${rtk.installerSha256}`);
    if (skillsVersion) args.push("--build-arg", `SKILLS_VERSION=${skillsVersion}`);
    run([...args, assets], { inherit: true });
  }
  if (command === "build") { build(); return; }
  if (command === "list") {
    run(["docker", "ps", "-a", "--filter", "label=dev.managed=true", "--format", "table {{.Label \"dev.project\"}}\t{{.Status}}\t{{.Ports}}"], { inherit: true });
    return;
  }
  if (!["start", "shell", "stop", "rebuild"].includes(command)) throw Error(`Unknown command: ${command}\n${help}`);
  const project = projectName(rawProject || "");
  let agent = "codex", repoOption: string | undefined, task: string | undefined, noAgent = false;
  let agentArgs: string[] = [];
  for (let i = 0; i < rest.length; i++) {
    const arg = rest[i];
    if (arg === "--") { agentArgs = rest.slice(i + 1); break; }
    if (arg === "--agent") agent = rest[++i];
    else if (arg === "--repo") repoOption = repository(rest[++i] || "");
    else if (arg === "--task") task = projectName(rest[++i] || "");
    else if (arg === "--no-agent") noAgent = true;
    else throw Error(`Unknown option: ${arg}`);
  }
  if (!["codex", "claude"].includes(agent)) throw Error("Agent must be codex or claude.");
  if (command !== "start" && (repoOption || task || noAgent || agent !== "codex")) throw Error("--repo, --task, --no-agent and --agent apply to dev start only.");
  const projectState = join(stateRoot, "projects", project);
  const source = join(root, project);
  for (const path of [source, projectState]) if (existsSync(path) && lstatSync(path).isSymbolicLink()) throw Error(`Refusing symlinked project/state directory: ${path}`);
  const metadataPath = join(projectState, "project.json");
  const composeProject = `dev-${project}-${createHash("sha256").update(source).digest("hex").slice(0, 8)}`;
  const workspaceImage = `${image}-${composeProject}`;
  let metadata: Metadata | undefined = existsSync(metadataPath) ? JSON.parse(readFileSync(metadataPath, "utf8")) : undefined;
  if (metadata && (metadata.source !== source || metadata.composeProject !== composeProject)) throw Error("Project location changed; restore its original DEV_ROOT and DEV_STATE_ROOT.");

  const git = (...args: string[]) => run(["git", "-C", source, ...args]).stdout;
  function compose(args: string[], inherit = false, allowFailure = false) {
    if (!existsSync(configExport)) mkdirSync(emptyConfig, { recursive: true, mode: 0o700 });
    const env = {
      DEV_IMAGE: metadata?.image || workspaceImage, DEV_BASE_IMAGE: image, DEV_PROJECT: project, DEV_SOURCE: source,
      DEV_PROJECT_STATE: projectState, DEV_ACCOUNTS: accounts, DEV_CREDENTIALS: credentials,
      DEV_AGENT_CONFIG: existsSync(configExport) ? realpathSync(configExport) : emptyConfig,
      DEV_UID: String(process.getuid!()), DEV_GID: String(process.getgid!()),
      DEV_GIT_NAME: run(["git", "config", "--get", "user.name"], { allowFailure: true }).stdout || "Development Agent",
      DEV_GIT_EMAIL: run(["git", "config", "--get", "user.email"], { allowFailure: true }).stdout || "agent@localhost",
    };
    const files = ["-f", join(assets, "compose.yaml")];
    const override = join(source, ".dev/compose.yaml");
    if (existsSync(override)) files.push("-f", override);
    return run(["docker", "compose", "--project-directory", source, "-p", composeProject, ...files, ...args], { env, inherit, allowFailure });
  }

  if (command === "shell") {
    if (!metadata) throw Error("Project not initialized. Run dev start PROJECT first.");
    const tty = process.stdin.isTTY && process.stdout.isTTY ? [] : ["-T"];
    const result = compose(["exec", ...tty, "--user", `${process.getuid!()}:${process.getgid!()}`, "dev", ...(agentArgs.length ? agentArgs : ["bash"])], true, true);
    process.exitCode = result.code; return;
  }
  if (command === "stop") {
    if (!metadata) throw Error("Project not initialized.");
    compose(["stop"], true); return;
  }

  mkdirSync(projectState, { recursive: true, mode: 0o700 });
  // Hold the host lock through the session so another start cannot change its checkout.
  if (process.env.DEV_LOCK_HELD !== project) {
    const child = run(["flock", "-n", "-E", "73", join(projectState, "startup.lock"), process.execPath, import.meta.path, ...argv], {
      inherit: true, allowFailure: true, env: { DEV_LOCK_HELD: project },
    });
    if (child.code === 73) throw Error("Project preparation already in progress or agent session active. Use dev shell, or dev stop to end it.");
    process.exitCode = child.code;
    return;
  }
  if (metadata) {
    const running = compose(["ps", "--status", "running", "-q", "dev"]).stdout;
    if (running && compose(["exec", "-T", "--user", `${process.getuid!()}:${process.getgid!()}`, "dev", "flock", "-n", "/home/node/.agent-session.lock", "true"], false, true).code) throw Error("Agent active; finish its session before starting or rebuilding.");
  }
  if (command === "rebuild") {
    if (!metadata) throw Error("Project not initialized.");
    build();
    const previous = { ...metadata };
    metadata.image = workspaceImage;
    metadata.baseImage = image;
    try {
      const config = JSON.parse(compose(["config", "--format", "json"]).stdout);
      if (!config.services.dev.build) run(["docker", "image", "tag", image, workspaceImage]);
      compose(["up", "-d", "--build", "--force-recreate", "--wait"], true);
    } catch (error) { metadata = previous; throw error; }
    atomic(metadataPath, JSON.stringify(metadata, null, 2) + "\n");
    console.log(`${project} rebuilt; source, home, caches, and account state preserved.`);
    return;
  }

  const started = performance.now();
  run(["docker", "info", "--format", "{{.ServerVersion}}"]);
  if (!existsSync(join(source, ".git")) && existsSync(source) && readdirSync(source).length) throw Error(`Project directory ${source} is nonempty and is not a Git repository; move it or initialize it explicitly.`);
  const bootstrap = join(projectState, "bootstrap-push");
  const repo = repoOption || metadata?.repo || (existsSync(join(source, ".git")) ? githubRemote(git("remote", "get-url", "origin")) : `${process.env.DEV_GITHUB_OWNER || "amahi2001"}/${project}`);
  repository(repo);
  if (metadata && metadata.repo.toLowerCase() !== repo.toLowerCase()) throw Error(`Project already belongs to ${metadata.repo}; use a different project name for ${repo}.`);
  const privateDefault = metadata?.privateDefault ?? (!repoOption && (existsSync(bootstrap) || !existsSync(join(source, ".git"))));
  const needsProvision = !metadata;
  // Warm starts use saved local identity; token retrieval performs no GitHub network request.
  if (needsProvision) {
    const result = run(["gh", "api", `repos/${repo}`], { allowFailure: true });
    if (result.code === 0) {
      const found = JSON.parse(result.stdout);
      if (found.full_name.toLowerCase() !== repo.toLowerCase()) throw Error("GitHub redirected to another repository; assign its canonical name explicitly.");
      if (privateDefault && !found.private) throw Error(`Default repository ${repo} exists but is public. Assign it explicitly or choose a different project name.`);
    } else {
      if (!result.stderr.includes("HTTP 404")) throw Error(`Cannot verify GitHub repository: ${result.stderr}`);
      if (repoOption) throw Error(`Assigned repository ${repo} is missing or inaccessible. No replacement created.`);
      const login = JSON.parse(run(["gh", "api", "user"]).stdout).login;
      if (repo.split("/")[0].toLowerCase() !== login.toLowerCase()) throw Error("Default owner must match the authenticated GitHub account.");
      run(["gh", "repo", "create", repo, "--private"], { inherit: true });
      const created = JSON.parse(run(["gh", "api", `repos/${repo}`]).stdout);
      if (!created.private || created.full_name.toLowerCase() !== repo.toLowerCase()) throw Error("Repository privacy/identity verification failed.");
    }
  }

  for (const dir of [root, projectState, join(projectState, "home"), accounts, join(accounts, "codex"), join(accounts, "claude"), credentials]) {
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    if (dir !== root) chmodSync(dir, 0o700);
  }
  const token = run(["gh", "auth", "token", "--hostname", "github.com"]).stdout;
  if (!token) throw Error("GitHub token unavailable. Run gh auth login on the host.");
  atomic(join(credentials, "github-token"), token + "\n");
  const accountAuth = join(accounts, "codex/auth.json");
  const hostAuth = join(process.env.CODEX_HOME || join(homedir(), ".codex"), "auth.json");
  if (!existsSync(accountAuth) && existsSync(hostAuth)) { copyFileSync(hostAuth, accountAuth); chmodSync(accountAuth, 0o600); }
  const codexConfig = join(accounts, "codex/config.toml");
  if (!existsSync(codexConfig)) atomic(codexConfig, 'cli_auth_credentials_store = "file"\n');

  if (!existsSync(join(source, ".git"))) {
    mkdirSync(source, { recursive: true });
    const remoteRefs = run(["git", "ls-remote", `https://github.com/${repo}.git`], {
      env: { GH_TOKEN: token, GIT_CONFIG_COUNT: "1", GIT_CONFIG_KEY_0: "credential.https://github.com.helper", GIT_CONFIG_VALUE_0: "!gh auth git-credential" },
    }).stdout;
    if (remoteRefs) run(["gh", "repo", "clone", repo, source], { inherit: true });
    else {
      git("init", "-b", "main");
      git("remote", "add", "origin", `https://github.com/${repo}.git`);
      writeFileSync(join(source, "README.md"), `# ${project}\n\nAgent development workspace. Run \`dev start ${project}\` on the server.\n`);
      writeFileSync(join(source, ".gitignore"), ".env\n.env.*\n!.env.example\n*.key\n*.pem\n*.secret\nnode_modules/\n.venv/\n__pycache__/\n.codex/\n.claude/\n.claude.json\n.dev-state/\n");
      git("add", "--", "README.md", ".gitignore");
      git("commit", "-m", "Initialize private development workspace");
      atomic(bootstrap, repo);
    }
  }
  if (githubRemote(git("remote", "get-url", "origin")).toLowerCase() !== repo.toLowerCase()) throw Error("Local origin differs from the assigned repository; reconcile it before starting.");
  if (existsSync(bootstrap)) {
    if (readFileSync(bootstrap, "utf8") !== repo) throw Error("Pending bootstrap belongs to another repository; preserve its local commit and reconcile the remote.");
    run(["git", "-C", source, "-c", "credential.https://github.com.helper=!gh auth git-credential", "push", "-u", "origin", "main"], { inherit: true });
    unlinkSync(bootstrap);
  }
  const branch = git("symbolic-ref", "--quiet", "--short", "HEAD");
  if (task || !branch.startsWith("agent/")) {
    if (task && git("status", "--porcelain")) throw Error("Uncommitted changes present. Resume without --task or finish them first.");
    const name = `agent/${task || "work"}-${new Date().toISOString().replace(/[-:.TZ]/g, "")}-${process.pid}`;
    git("switch", "-c", name);
  }
  const excludes = resolve(source, git("rev-parse", "--git-path", "info/exclude"));
  const existingExcludes = existsSync(excludes) ? readFileSync(excludes, "utf8") : "";
  if (!existingExcludes.includes("# dev runtime")) atomic(excludes, existingExcludes + "\n# dev runtime\n.env\n.env.*\n!.env.example\n.codex/\n.claude/\n.claude.json\nauth.json\nnode_modules/\n.venv/\n", 0o644);
  if (run(["docker", "image", "inspect", image], { allowFailure: true }).code) build();
  const workspaceExists = run(["docker", "image", "inspect", workspaceImage], { allowFailure: true }).code === 0;
  metadata = { repo, privateDefault, source, composeProject, image: workspaceImage, baseImage: image };
  if (!workspaceExists) {
    if (existsSync(join(source, ".dev/compose.yaml")) && JSON.parse(compose(["config", "--format", "json"]).stdout).services.dev.build) compose(["build", "dev"], true);
    else run(["docker", "image", "tag", image, workspaceImage]);
  }
  atomic(metadataPath, JSON.stringify(metadata, null, 2) + "\n");
  compose(["up", "-d", "--wait", "--no-build"], true);
  console.log(`${project} ready in ${((performance.now() - started) / 1000).toFixed(2)}s | ${repo} | ${git("branch", "--show-current")}`);
  console.log(`Source: ${source}\nPreview mappings: dev list | Shell: dev shell ${project}`);
  if (!noAgent) {
    const tty = process.stdin.isTTY && process.stdout.isTTY ? [] : ["-T"];
    process.exitCode = compose(["exec", ...tty, "--user", `${process.getuid!()}:${process.getgid!()}`, "dev", "dev-agent", agent, ...agentArgs], true, true).code;
  }
}

if (import.meta.main) {
  try { main(); }
  catch (error) { console.error(`dev: ${error instanceof Error ? error.message : error}`); process.exitCode = 1; }
}
