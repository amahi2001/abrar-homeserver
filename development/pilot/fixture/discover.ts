// Read native discovery metadata; Claude's model endpoint is deliberately unusable.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const selected = new Set([...Object.keys(JSON.parse(readFileSync("/opt/agent-config/skills-lock.json", "utf8")).skills), "playwright-cli"]);
function found(value: any): string[] {
  const names = new Set<string>();
  function visit(item: any) {
    if (Array.isArray(item)) { for (const child of item) visit(child); }
    else if (item && typeof item === "object") {
      if (selected.has(item.name)) names.add(item.name);
      for (const child of Object.values(item)) visit(child);
    } else if (typeof item === "string" && selected.has(item)) names.add(item);
  }
  visit(value);
  return [...names].sort();
}
async function readUntil(child: ReturnType<typeof Bun.spawn>, label: string, handle: (message: any) => string[] | undefined) {
  let timer: ReturnType<typeof setTimeout>;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => { child.kill("SIGKILL"); reject(Error(`${label} discovery timed out after 20 seconds`)); }, 20000);
  });
  try {
    return await Promise.race([deadline, (async () => {
      let buffer = "";
      const decoder = new TextDecoder();
      for await (const bytes of child.stdout as ReadableStream<Uint8Array>) {
        buffer += decoder.decode(bytes, { stream: true });
        let end: number;
        while ((end = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
          let message;
          try { message = JSON.parse(line); } catch { continue; }
          const names = handle(message);
          if (names) {
            for (const name of ["playwright-cli", "ponytail", "caveman"]) assert(names.includes(name), `${label} did not discover ${name}`);
            return names;
          }
        }
      }
      throw Error(`${label} exited before reporting its skills`);
    })()]);
  } finally {
    clearTimeout(timer!);
    child.kill("SIGKILL");
    await child.exited;
  }
}
const codex = Bun.spawn(["codex", "app-server"], { stdin: "pipe", stdout: "pipe", stderr: "ignore" });
const send = (message: unknown) => codex.stdin.write(JSON.stringify(message) + "\n");
send({ id: 1, method: "initialize", params: { clientInfo: { name: "devcontainer-pilot", version: "1.0" } } });
const codexNames = await readUntil(codex, "Codex", message => {
  if (message.id === 1) {
    assert(!message.error, "Codex initialization failed");
    send({ method: "initialized" });
    send({ id: 2, method: "skills/list", params: { cwds: [process.cwd()], forceReload: true } });
  }
  if (message.id === 2) {
    assert(message.result && !message.error, "Codex skills/list failed");
    return found(message.result);
  }
});
const claude = Bun.spawn(["claude", "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence", "--max-turns", "1", "Reply OK"], {
  stdin: "ignore", stdout: "pipe", stderr: "ignore",
  env: { ...process.env, ANTHROPIC_API_KEY: "pilot-no-model-access", ANTHROPIC_AUTH_TOKEN: "", ANTHROPIC_BASE_URL: "http://127.0.0.1:9", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1", DISABLE_AUTOUPDATER: "1" },
});
const claudeNames = await readUntil(claude, "Claude", message => message.type === "system" && message.subtype === "init" ? found([message.skills || [], message.slash_commands || []]) : undefined);
console.log(JSON.stringify({ codex: codexNames, claude: claudeNames }));
