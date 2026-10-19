import { expect, test } from "bun:test";
import { mkdtemp, mkdir, chmod, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Every child gets only synthetic credentials, a temporary profile directory,
// and a PATH containing this fake bw. These tests cannot invoke the real CLI.
async function sync(fixture: Record<string, any>, options: { missingCredentials?: boolean; missingBw?: boolean } = {}) {
  const dir = await mkdtemp(join(tmpdir(), "bitwarden-sync-test-"));
  try {
    const bin = join(dir, "bin");
    await mkdir(bin);
    await writeFile(join(dir, "fixture.json"), JSON.stringify(fixture));
    if (!options.missingBw) {
      await writeFile(join(bin, "bw"), String.raw`#!${process.execPath}
import { readFileSync, appendFileSync, existsSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";
const dir = process.env.FAKE_BW_DIR;
const fixture = JSON.parse(readFileSync(join(dir, "fixture.json"), "utf8"));
const args = process.argv.slice(2);
const side = basename(process.env.BITWARDENCLI_APPDATA_DIR);
const call = { side, args, clientId: process.env.BW_CLIENTID };
if (args[0] === "create" || args[0] === "edit") {
  call.payload = JSON.parse(Buffer.from(readFileSync(0, "utf8").trim(), "base64").toString("utf8"));
}
appendFileSync(join(dir, "calls.jsonl"), JSON.stringify(call) + "\n");
if (args[0] === "edit" && fixture.editError) {
  const counter = join(dir, "attempts");
  const attempt = (existsSync(counter) ? Number(readFileSync(counter, "utf8")) : 0) + 1;
  writeFileSync(counter, String(attempt));
  if (attempt <= (fixture.editFailures ?? 1)) { console.error(fixture.editError); process.exit(1); }
}
if (args[0] === "unlock") console.log('export BW_SESSION="fake-' + side + '-session"');
else if (args[0] === "list") {
  if (fixture.invalidJson) console.log("invalid json");
  else console.log(JSON.stringify(fixture[side]?.[args[1]] ?? []));
} else if (args[0] === "create") console.log(JSON.stringify({ ...call.payload, id: "created-" + args[1] }));
else if (args[0] === "logout" && fixture.loggedOut) { console.error("already logged out"); process.exit(1); }
`);
      await chmod(join(bin, "bw"), 0o700);
    }
    const env: Record<string, string> = {
      PATH: bin,
      HOME: dir,
      TMPDIR: dir,
      BITWARDEN_SYNC_STATE_DIR: join(dir, "profiles"),
      FAKE_BW_DIR: dir,
      BW_CLIENTID: "fake-cloud-client",
      BW_CLIENTSECRET: "fake-cloud-secret",
      BW_PASSWORD: "fake-cloud-password",
      VW_CLIENTID: "fake-vault-client",
      VW_CLIENTSECRET: "fake-vault-secret",
      VW_PASSWORD: "fake-vault-password",
      VW_SERVER_URL: "https://vault.invalid",
    };
    if (options.missingCredentials) delete env.BW_PASSWORD;
    const runtime = process.env.SYNC_TEST_RUNTIME || process.execPath;
    const script = process.env.SYNC_TEST_SCRIPT || join(import.meta.dir, "bitwarden-sync.ts");
    const child = Bun.spawn([runtime, ...(process.env.SYNC_TEST_RUNTIME_ARGS?.split(" ") || ["run"]), script], {
      env, stdout: "pipe", stderr: "pipe",
    });
    const [stdout, stderr, code] = await Promise.all([
      new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
    ]);
    const calls = await readFile(join(dir, "calls.jsonl"), "utf8").catch(() => "");
    return { stdout, stderr, code, calls: calls.trim() ? calls.trim().split("\n").map(line => JSON.parse(line)) : [] };
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

const source = (extra = {}) => ({ id: "source-1", name: "Example", type: 1, login: { username: "user", password: "secret" }, ...extra });
const tracked = (extra = {}) => ({ ...source(), id: "target-1", fields: [{ name: "bw_id", value: "source-1", type: 0 }], ...extra });
const mutations = (calls: any[]) => calls.filter(c => ["create", "edit", "delete"].includes(c.args[0]));

test("unchanged items cause no writes; server setup and credentials retain their direction", async () => {
  const result = await sync({ loggedOut: true, src: { items: [source()] }, dst: { items: [tracked()] } });
  expect(result.code, result.stderr).toBe(0);
  expect(mutations(result.calls)).toEqual([]);
  expect(result.stdout).toContain("0 created, 0 updated, 0 adopted, 0 deleted");
  expect(result.calls.filter(c => c.args[0] === "config").map(c => [c.side, c.args[2]])).toEqual([
    ["src", "https://vault.invalid"], ["dst", "https://bitwarden.com"],
  ]);
  expect(result.calls.filter(c => c.args[0] === "login").map(c => [c.side, c.clientId])).toEqual([
    ["src", "fake-vault-client"], ["dst", "fake-cloud-client"],
  ]);
});

test("folder creation and large Unicode item payload survive piped stdin", async () => {
  const notes = "秘密 🔐 café\n".repeat(12000);
  const result = await sync({ src: { folders: [{ id: "folder-1", name: "秘密" }], items: [source({ folderId: "folder-1", notes })] } });
  expect(result.code, result.stderr).toBe(0);
  const writes = mutations(result.calls);
  expect(writes.map(c => [c.side, c.args[0], c.args[1]])).toEqual([["dst", "create", "folder"], ["dst", "create", "item"]]);
  expect(writes[1].payload.notes).toBe(notes);
  expect(writes[1].payload.folderId).toBe("created-folder");
  expect(writes[1].payload.fields).toContainEqual({ name: "bw_id", value: "source-1", type: 0 });
});

test("tracked changes update the existing target and matching folder", async () => {
  const result = await sync({ src: { items: [source({ folderId: "src-folder" })], folders: [{ id: "src-folder", name: "WORK" }] }, dst: { items: [tracked({ login: { username: "user", password: "old" } })], folders: [{ id: "dst-folder", name: "work" }] } });
  expect(result.code, result.stderr).toBe(0);
  const writes = mutations(result.calls);
  expect(writes).toHaveLength(1);
  expect(writes[0].args.slice(0, 3)).toEqual(["edit", "item", "target-1"]);
  expect(writes[0].payload.folderId).toBe("dst-folder");
  expect(writes[0].payload.login.password).toBe("secret");
});

test("manual imports are adopted rather than duplicated", async () => {
  const result = await sync({ src: { items: [source()] }, dst: { items: [source({ id: "manual-1" })] } });
  expect(result.code, result.stderr).toBe(0);
  expect(mutations(result.calls).map(c => c.args.slice(0, 3))).toEqual([["edit", "item", "manual-1"]]);
  expect(result.stdout).toContain("1 adopted");
});

test("only missing tracked items are deleted; untracked target items remain", async () => {
  const result = await sync({ dst: { items: [tracked(), source({ id: "untracked" })] } });
  expect(result.code, result.stderr).toBe(0);
  expect(mutations(result.calls).map(c => [c.side, ...c.args.slice(0, 3)])).toEqual([["dst", "delete", "item", "target-1"]]);
});

test("transient edit failure retries the same payload successfully", async () => {
  const result = await sync({ src: { items: [source()] }, dst: { items: [tracked({ notes: "old" })] }, editError: "ECONNRESET" });
  expect(result.code, result.stderr).toBe(0);
  const writes = mutations(result.calls);
  expect(writes).toHaveLength(2);
  expect(writes[0].payload).toEqual(writes[1].payload);
  expect(result.stderr).toContain("retrying");
}, 10000);

test("permanent edit failure exits nonzero without leaking session arguments or deleting", async () => {
  const result = await sync({ src: { items: [source()] }, dst: { items: [tracked({ notes: "old" }), tracked({ id: "obsolete", fields: [{ name: "bw_id", value: "deleted" }] })] }, editError: "permission denied" });
  expect(result.code).toBe(1);
  expect(mutations(result.calls).map(c => c.args[0])).toEqual(["edit"]);
  expect(result.stderr).toContain("[REDACTED]");
  expect(result.stderr).not.toContain("fake-dst-session");
});

test("invalid CLI JSON fails before any vault mutation", async () => {
  const result = await sync({ invalidJson: true });
  expect(result.code).toBe(1);
  expect(mutations(result.calls)).toEqual([]);
});

test("missing credentials fail before invoking bw", async () => {
  const result = await sync({}, { missingCredentials: true });
  expect(result.code).toBe(1);
  expect(result.stderr).toContain("Missing required environment variables");
  expect(result.calls).toEqual([]);
});

test("missing bw executable exits cleanly with a useful error", async () => {
  const result = await sync({}, { missingBw: true });
  expect(result.code).toBe(1);
  expect(result.stderr).toContain("ENOENT");
  expect(result.calls).toEqual([]);
});
