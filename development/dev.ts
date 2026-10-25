#!/usr/bin/env bun
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync, renameSync, chmodSync, copyFileSync, readdirSync, lstatSync, unlinkSync, realpathSync, readlinkSync, symlinkSync, openSync, closeSync, fstatSync, constants } from "node:fs";
import { homedir, hostname, userInfo } from "node:os";
import { join, resolve, dirname } from "node:path";

const help = `Usage:
  dev start PROJECT [--agent codex|claude] [--repo OWNER/REPO] [--task NAME] [--no-agent] [-- agent arguments]
  dev shell PROJECT [-- command arguments]
  dev stop PROJECT
  dev view PROJECT [browser|desktop] [--stop]
  dev list
  dev setup [--refresh-runtime]
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

export function withAgentDispatchLock<T>(home: string, action: () => T): T {
  if (!lstatSync(home).isDirectory()) throw Error("Cannot verify the shared project home directory.");
  const path = join(home, ".agent-session.lock");
  const fd = openSync(path, constants.O_CREAT | constants.O_RDWR | constants.O_NOFOLLOW, 0o600);
  try {
    if (!fstatSync(fd).isFile()) throw Error("Invalid project agent lock.");
    // flock locks the shared open-file description: the parent's FD keeps it
    // held after this helper exits, even while the container is replaced.
    const lock = Bun.spawnSync(["flock", "-n", "0"], { stdin: fd, stdout: "pipe", stderr: "pipe" });
    if (lock.exitCode !== 0) throw Error("Agent active or dispatch lock unavailable; preserving the running container.");
    const current = lstatSync(path), held = fstatSync(fd);
    if (current.dev !== held.dev || current.ino !== held.ino) throw Error("Project agent lock changed; retry when idle.");
    return action();
  } finally { closeSync(fd); }
}

function deploymentLock(project?: string): () => void {
  if (project && process.env.DEV_HOST_LOCK_HELD === project) return () => {};
  let fd: number | undefined = openSync(process.env.DEV_DEPLOYMENT_LOCK || "/run/lock/host-deployment.lock", constants.O_CREAT | constants.O_RDWR | constants.O_NOFOLLOW, 0o660);
  const release = () => { if (fd !== undefined) { closeSync(fd); fd = undefined; } };
  try {
    const lock = Bun.spawnSync(["flock", "-s", "0"], { stdin: fd, stdout: "pipe", stderr: "pipe" });
    if (lock.exitCode !== 0 || !fstatSync(fd).isFile()) throw Error("Cannot acquire the host deployment lock; retry after maintenance.");
    return release;
  } catch (error) { release(); throw error; }
}

type Metadata = { repo: string; privateDefault: boolean; source: string; composeProject: string; image: string; baseImage?: string; configHash?: string };

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
  const toolVersions = existsSync(toolsLock) ? JSON.parse(readFileSync(toolsLock, "utf8")) : {};
  const nativeVersion = toolVersions.devcontainerCli?.version ?? "0.89.0";
  if (typeof nativeVersion !== "string" || !/^\d+\.\d+\.\d+$/.test(nativeVersion)) throw Error("Invalid locked Dev Containers CLI version");
  const nativePrefix = join(homedir(), ".local/share/devcontainers");
  const nativeCli = process.env.DEVCONTAINER_CLI || join(nativePrefix, "bin/devcontainer");
  function nativeReady() {
    if (!Bun.which(nativeCli)) throw Error(`Dev Containers CLI ${nativeVersion} required; run dev setup.`);
    const version = run([nativeCli, "--version"], { allowFailure: true }).stdout;
    if (version !== nativeVersion) throw Error(`Dev Containers CLI ${nativeVersion} required; run dev setup. Found: ${version || "not installed"}`);
  }
  if (command === "setup") {
    if ((rawProject && rawProject !== "--refresh-runtime") || rest.length) throw Error("Usage: dev setup [--refresh-runtime]");
    const refreshRuntime = rawProject === "--refresh-runtime";
    const lock = join(homedir(), ".local/share/devcontainers.setup.lock");
    mkdirSync(dirname(lock), { recursive: true, mode: 0o700 });
    if (!process.env.DEV_SETUP_LOCKED) {
      process.exitCode = run(["flock", lock, process.execPath, import.meta.path, ...argv], { inherit: true, allowFailure: true, env: { DEV_SETUP_LOCKED: "1" } }).code;
      return;
    }
    if (!refreshRuntime && existsSync(nativeCli) && run([nativeCli, "--version"], { allowFailure: true }).stdout === nativeVersion) { console.log(`Dev Containers CLI ${nativeVersion} ready.`); return; }
    const installer = `${lock}.install.sh`;
    const previousLinks = ["node/current", "cli/current"].map(relative => {
      const path = join(nativePrefix, relative);
      return { path, target: existsSync(path) && lstatSync(path).isSymbolicLink() ? readlinkSync(path) : undefined };
    });
    try {
      run(["curl", "-fsSL", "https://raw.githubusercontent.com/devcontainers/cli/main/scripts/install.sh", "-o", installer]);
      run(["sh", installer, "--version", nativeVersion, "--node-version", "24", "--prefix", nativePrefix], { inherit: true });
      nativeReady();
      const version = run([join(nativePrefix, "node/current/bin/node"), "--version"]).stdout;
      const parts = /^v24\.(\d+)\.(\d+)$/.exec(version);
      if (!parts || Number(parts[1]) < 22 || (Number(parts[1]) === 22 && Number(parts[2]) < 1)) throw Error(`Patched Node.js 24 required; installer returned ${version}`);
    } catch (error) {
      for (const { path, target } of previousLinks) if (target) {
        const temp = `${path}.${process.pid}.rollback`;
        symlinkSync(target, temp); renameSync(temp, path);
      }
      throw error;
    } finally { if (existsSync(installer)) unlinkSync(installer); }
    return;
  }
  const rtk = toolVersions.rtk;
  const playwrightVersion = toolVersions.playwrightCli?.version ?? "0.1.22";
  if (typeof playwrightVersion !== "string" || !/^\d+\.\d+\.\d+$/.test(playwrightVersion)) throw Error("Invalid locked Playwright CLI version");
  const codebaseMemory = toolVersions.codebaseMemory ?? {
    version: "0.12.1",
    installer: "https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/a7158ac2f48ca646b4527341b76f930f2aeff87e/install.sh",
    installerSha256: "13049c7cc51bc508d68b8ecb8a9fd9574ecb7c6f2c9dd5a19bf7d4c187321145",
  };
  if (typeof codebaseMemory.version !== "string" || !/^\d+\.\d+\.\d+$/.test(codebaseMemory.version)
      || typeof codebaseMemory.installer !== "string" || !/^https:\/\/raw\.githubusercontent\.com\/DeusData\/codebase-memory-mcp\/[a-f0-9]{40}\/install\.sh$/.test(codebaseMemory.installer)
      || typeof codebaseMemory.installerSha256 !== "string" || !/^[a-f0-9]{64}$/.test(codebaseMemory.installerSha256)) throw Error("Invalid locked Codebase Memory release");
  const personalPackage = join(configExport, "package.json");
  const skillsVersion = existsSync(personalPackage) ? JSON.parse(readFileSync(personalPackage, "utf8")).dependencies.skills : undefined;
  const fingerprint = createHash("sha256");
  if (rtk) fingerprint.update(JSON.stringify(rtk));
  if (skillsVersion) fingerprint.update(skillsVersion);
  fingerprint.update(playwrightVersion);
  fingerprint.update(JSON.stringify(codebaseMemory));
  function hashFiles(dir: string) {
    for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) hashFiles(path);
      else if (entry.isFile()) { fingerprint.update(path.slice(assets.length)); fingerprint.update(readFileSync(path)); }
    }
  }
  for (const file of ["Dockerfile", "entrypoint.sh", "agent-session.sh", "dev-finish.sh", "gh.sh", "instructions.md", "dev-ui.sh", "ui.md", "playwright-cli.sh", "browser-state.cjs", "codebase-memory.ts"]) fingerprint.update(readFileSync(join(assets, file)));
  if (existsSync(join(assets, "skills"))) hashFiles(join(assets, "skills"));
  const image = `buildfleet-dev:${fingerprint.digest("hex").slice(0, 16)}`;

  function build() {
    const args = ["docker", "build", "--tag", image, "--pull", "--build-arg", `APT_REFRESH=${Math.floor(Date.now() / (7 * 86400000))}`, "--build-arg", `PLAYWRIGHT_CLI_VERSION=${playwrightVersion}`];
    args.push("--build-arg", `CODEBASE_MEMORY_VERSION=${codebaseMemory.version}`, "--build-arg", `CODEBASE_MEMORY_INSTALLER=${codebaseMemory.installer}`, "--build-arg", `CODEBASE_MEMORY_INSTALLER_SHA256=${codebaseMemory.installerSha256}`);
    if (rtk) args.push("--build-arg", `RTK_VERSION=v${rtk.version.replace(/^v/, "")}`, "--build-arg", `RTK_INSTALLER=${rtk.installer}`, "--build-arg", `RTK_INSTALLER_SHA256=${rtk.installerSha256}`);
    if (skillsVersion) args.push("--build-arg", `SKILLS_VERSION=${skillsVersion}`);
    run([...args, assets], { inherit: true });
  }
  if (command === "build") {
    const release = deploymentLock();
    try { build(); console.log(image); return; }
    finally { release(); }
  }
  if (command === "list") {
    console.log("Viewer mappings: browser = 8931/tcp | desktop = 6080/tcp (SSH tunnel required)");
    run(["docker", "ps", "-a", "--filter", "label=dev.managed=true", "--format", "table {{.Label \"dev.project\"}}\t{{.Status}}\t{{.Ports}}"], { inherit: true });
    return;
  }
  if (!["start", "shell", "stop", "rebuild", "view"].includes(command)) throw Error(`Unknown command: ${command}\n${help}`);
  const project = projectName(rawProject || "");
  let agent = "codex", repoOption: string | undefined, task: string | undefined, noAgent = false;
  let agentArgs: string[] = [];
  let viewer = "browser", stopViewer = false;
  if (command === "view") {
    const positional = rest.filter(a => a !== "--stop");
    if (positional.length > 1 || (positional.length && !["browser", "desktop"].includes(positional[0])) || rest.filter(a => a === "--stop").length > 1) throw Error("Usage: dev view PROJECT [browser|desktop] [--stop]");
    viewer = positional[0] || "browser";
    stopViewer = rest.includes("--stop");
  }
  for (let i = 0; command !== "view" && i < rest.length; i++) {
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
  function environment() {
    if (!existsSync(configExport)) mkdirSync(emptyConfig, { recursive: true, mode: 0o700 });
    return {
      DEV_IMAGE: metadata?.image || workspaceImage, DEV_BASE_IMAGE: image, DEV_PROJECT: project, DEV_SOURCE: source,
      DEV_PROJECT_STATE: projectState, DEV_ACCOUNTS: accounts, DEV_CREDENTIALS: credentials,
      DEV_AGENT_CONFIG: existsSync(configExport) ? realpathSync(configExport) : emptyConfig,
      DEV_UID: String(process.getuid!()), DEV_GID: String(process.getgid!()),
      DEV_GIT_NAME: run(["git", "config", "--get", "user.name"], { allowFailure: true }).stdout || "Development Agent",
      DEV_GIT_EMAIL: run(["git", "config", "--get", "user.email"], { allowFailure: true }).stdout || "agent@localhost",
      COMPOSE_PROJECT_NAME: composeProject,
    };
  }
  function compose(args: string[], inherit = false) {
    const files = ["-f", join(assets, "compose.yaml")];
    const override = join(source, ".dev/compose.yaml");
    if (existsSync(override)) files.push("-f", override);
    return run(["docker", "compose", "--project-directory", source, "-p", composeProject, ...files, ...args], { env: environment(), inherit });
  }
  const configPath = join(projectState, "devcontainer.json");
  const nativeConfig = {
    name: project, dockerComposeFile: "compose.json", service: "dev",
    workspaceFolder: `/workspace/${project}`, remoteUser: "node", updateRemoteUserUID: false,
    overrideCommand: false, userEnvProbe: "none", shutdownAction: "none",
  };
  function configure(config: any) {
    atomic(join(projectState, "compose.json"), JSON.stringify(config, null, 2) + "\n");
    atomic(configPath, JSON.stringify(nativeConfig, null, 2) + "\n");
  }
  function native(command: string, args: string[] = [], inherit = false, allowFailure = false) {
    if (!existsSync(configPath)) configure(JSON.parse(compose(["config", "--format", "json"]).stdout));
    const identity = command === "build" ? [] : ["--id-label", `com.docker.compose.project=${composeProject}`, "--id-label", "com.docker.compose.service=dev"];
    return run([nativeCli, command, "--workspace-folder", source, "--config", configPath, "--user-data-folder", join(projectState, "devcontainers"), ...identity, ...args], { env: environment(), inherit, allowFailure });
  }
  function container() {
    const ids = run(["docker", "ps", "-q", "--filter", `label=com.docker.compose.project=${composeProject}`, "--filter", "label=com.docker.compose.service=dev"]).stdout.split("\n").filter(Boolean);
    if (ids.length > 1) throw Error("Multiple development containers found; reconcile the project before continuing.");
    return ids[0];
  }
  function checkpointBrowser() {
    if (!metadata || !container()) return;
    // Old images have no browser tooling; transport/checkpoint errors must fail.
    native("exec", ["bash", "-ec", "if [ -x /usr/local/bin/dev-ui ]; then dev-ui _checkpoint; fi"]);
  }
  function startContainer(force = false) {
    const config = JSON.parse(compose(["config", "--format", "json"]).stdout);
    const home = join(projectState, "home");
    const mounts = config.services.dev.volumes?.filter((mount: any) => mount.target === "/home/node");
    if (mounts?.length !== 1 || mounts[0].type !== "bind" || resolve(mounts[0].source) !== home) throw Error("Cannot verify the shared project home mount; preserving the running container.");
    const existing = container();
    if (existing) {
      const mounted = JSON.parse(run(["docker", "inspect", existing, "--format", "{{json .Mounts}}"]).stdout).filter((mount: any) => mount.Destination === "/home/node");
      if (mounted.length !== 1 || mounted[0].Type !== "bind" || mounted[0].Source !== home) throw Error("Running container has an unknown project home mount; preserving it.");
    }
    return withAgentDispatchLock(home, () => {
      const configHash = createHash("sha256").update(JSON.stringify(config)).update(JSON.stringify(nativeConfig)).digest("hex");
      const recreate = force || metadata?.configHash !== configHash;
      if (recreate) checkpointBrowser();
      if (config.services.dev.build && (force || run(["docker", "image", "inspect", config.services.dev.image], { allowFailure: true }).code)) {
        configure(config);
        native("build");
      }
      // The project image was prepared once; ordinary starts must never rebuild it.
      delete config.services.dev.build;
      configure(config);
      let id = container();
      if (recreate || !id || run(["docker", "inspect", id, "--format", "{{.State.Health.Status}}"]).stdout !== "healthy") {
        const result = native("up", recreate ? ["--remove-existing-container"] : []);
        id = JSON.parse(result.stdout).containerId;
        if (typeof id !== "string" || !id) throw Error("Dev Containers CLI returned no container ID");
      }
      // Native up skips Compose when dev is running; keep its sidecars running too.
      const sidecars = Object.keys(config.services).filter(service => service !== "dev");
      if (sidecars.length) run(["docker", "compose", "--project-directory", source, "-p", composeProject, "-f", join(projectState, "compose.json"), "up", "-d", "--no-build", "--no-deps", "--wait", ...sidecars], { env: environment() });
      // Give first-time indexing a bounded window; healthy warm boxes return immediately.
      for (let i = 0; i < 1200; i++) {
        const state = JSON.parse(run(["docker", "inspect", id, "--format", "{{json .State}}"]).stdout);
        if (state.Running && state.Health?.Status === "healthy") return configHash;
        if (!state.Running || state.Health?.Status === "unhealthy") break;
        Bun.sleepSync(500);
      }
      const logs = run(["docker", "logs", "--tail", "80", id], { allowFailure: true });
      throw Error(`Container did not become healthy.\n${logs.stdout}\n${logs.stderr}`);
    });
  }

  if (command === "view") {
    if (!metadata) throw Error("Project not initialized. Run dev start PROJECT --no-agent first.");
    const id = container();
    if (!id) throw Error("Project is not running. Run dev start PROJECT --no-agent first.");
    nativeReady();
    const port = viewer === "browser" ? "8931" : "6080";
    const mapping = run(["docker", "port", id, `${port}/tcp`]).stdout;
    const match = /^127\.0\.0\.1:(\d+)$/.exec(mapping);
    if (!match || Number(match[1]) < 1 || Number(match[1]) > 65535) throw Error(`Expected one host-loopback mapping for ${port}; found ${mapping || "none"}. Rebuild an idle box if viewer ports are missing.`);
    const target = process.env.DEV_SSH_TARGET || `${userInfo().username}@${hostname()}`;
    if (/[\r\n]/.test(target) || target.startsWith("-") || !target) throw Error("Invalid DEV_SSH_TARGET");
    native("exec", ["dev-ui", stopViewer ? "stop" : "start", viewer], true);
    if (!stopViewer) {
      const published = match[1];
      const quoted = "'" + target.replaceAll("'", "'\\''") + "'";
      console.log(`${viewer}: http://127.0.0.1:${published}/${viewer === "desktop" ? "vnc.html?autoconnect=1&resize=remote" : ""}`);
      console.log(`On your computer: ssh -N -L ${published}:127.0.0.1:${published} ${quoted}`);
    }
    return;
  }

  if (command === "shell") {
    if (!metadata) throw Error("Project not initialized. Run dev start PROJECT first.");
    nativeReady();
    const result = native("exec", agentArgs.length ? agentArgs : ["bash"], true, true);
    process.exitCode = result.code; return;
  }
  if (command === "stop") {
    if (!metadata) throw Error("Project not initialized.");
    const release = deploymentLock(project);
    try {
      nativeReady();
      checkpointBrowser();
      compose(["stop"], true); return;
    } finally { release(); }
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
  // Core deployment holds this mutex exclusively while enumerating/stopping
  // the fleet. Include new/stopped projects, then release before agent attach.
  const releasePreparation = deploymentLock(project);
  try {
    nativeReady();
    if (metadata) {
      const id = container();
      if (id && run(["docker", "exec", "--user", `${process.getuid!()}:${process.getgid!()}`, id, "flock", "-n", "/home/node/.agent-session.lock", "true"], { allowFailure: true }).code) throw Error("Agent active; finish its session before starting or rebuilding.");
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
        metadata.configHash = startContainer(true);
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
    const claudeAuth = join(accounts, "claude/.credentials.json");
    const hostClaudeAuth = join(process.env.CLAUDE_CONFIG_DIR || join(homedir(), ".claude"), ".credentials.json");
    if (!existsSync(claudeAuth) && existsSync(hostClaudeAuth)) { copyFileSync(hostClaudeAuth, claudeAuth); chmodSync(claudeAuth, 0o600); }
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
    metadata = { ...metadata, repo, privateDefault, source, composeProject, image: workspaceImage, baseImage: image };
    if (!workspaceExists && !JSON.parse(compose(["config", "--format", "json"]).stdout).services.dev.build) run(["docker", "image", "tag", image, workspaceImage]);
    // Retain repository identity on failed first startup; readiness is committed below.
    if (!existsSync(metadataPath)) atomic(metadataPath, JSON.stringify(metadata, null, 2) + "\n");
    metadata.configHash = startContainer();
    atomic(metadataPath, JSON.stringify(metadata, null, 2) + "\n");
    console.log(`${project} ready in ${((performance.now() - started) / 1000).toFixed(2)}s | ${repo} | ${git("branch", "--show-current")}`);
    console.log(`Source: ${source}\nPreview mappings: dev list | Shell: dev shell ${project}`);
    releasePreparation();
    if (!noAgent) {
      process.exitCode = native("exec", ["dev-agent", agent, ...agentArgs], true, true).code;
    }
  } finally { releasePreparation(); }
}

if (import.meta.main) {
  try { main(); }
  catch (error) { console.error(`dev: ${error instanceof Error ? error.message : error}`); process.exitCode = 1; }
}
