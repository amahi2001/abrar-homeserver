#!/usr/bin/env bun
// Synchronize the owner's personal Vaultwarden items to Bitwarden.
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { isDeepStrictEqual } from "node:util";

const state = process.env.BITWARDEN_SYNC_STATE_DIR || "/var/lib/bitwarden-sync";
class SyncError extends Error {}
class RetryableEdit extends SyncError {}
class MissingItem extends SyncError {}
function requireState(condition: unknown, message: string): asserts condition {
  if (!condition) throw new SyncError(message);
}

async function bw(profile: string, args: string[], env: Record<string, string> = {}, payload?: unknown): Promise<string> {
  const child = spawn("bw", args, {
    env: { ...process.env, BITWARDENCLI_APPDATA_DIR: profile, BW_NOINTERACTION: "true", ...env },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const output: Buffer[] = [];
  const errors: Buffer[] = [];
  child.stdout.on("data", (b: Buffer) => output.push(b));
  child.stderr.on("data", (b: Buffer) => errors.push(b));
  return new Promise((resolve, reject) => {
    child.once("error", (e: NodeJS.ErrnoException) =>
      reject(new SyncError(e.code === "ENOENT" ? "bw executable missing (ENOENT)" : "bw could not start")));
    child.stdin.on("error", () => {}); // CLI rejection is reported by close.
    child.on("close", code => {
      if (code === 0) return resolve(Buffer.concat(output).toString().trim());
      const detail = Buffer.concat(errors).toString().trim();
      if (code === 1 && args[0] === "get" && args[1] === "item" && detail === "Not found.") return reject(new MissingItem("Source item absent"));
      const transient = /FetchError|network|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|socket hang up|\b(?:429|500|502|503|504)\b/i.test(detail);
      const ErrorType = args[0] === "edit" && transient ? RetryableEdit : SyncError;
      reject(new ErrorType(`bw ${args[0]} failed`)); // Neither CLI output nor item metadata belongs in the journal.
    });
    child.stdin.end(payload === undefined ? undefined : Buffer.from(JSON.stringify(payload)).toString("base64"));
  });
}

async function openVault(profile: string, url: string, passwordVariable: string, id?: string, secret?: string, email?: string) {
  url = new URL(url.includes("://") ? url : `https://${url}`).href.replace(/\/$/, "");
  await mkdir(profile, { recursive: true, mode: 0o700 });
  await bw(profile, ["logout"]).catch(() => {}); // Locked and logged-out profiles can both occur.
  await bw(profile, ["config", "server", url]);
  if (id && secret) await bw(profile, ["login", "--apikey"], { BW_CLIENTID: id, BW_CLIENTSECRET: secret });
  else {
    try { await bw(profile, ["login", email!, "--passwordenv", passwordVariable]); }
    catch { throw new SyncError("Email login failed. If device verification is enabled, configure the source API credentials for unattended sync and preview."); }
  }
  const session = await bw(profile, ["unlock", "--passwordenv", passwordVariable, "--raw"]);
  requireState(session && !/\s/.test(session), "Could not unlock vault");
  const command = (args: string[], payload?: unknown) => bw(profile, args, { BW_SESSION: session }, payload);
  const info = JSON.parse(await command(["status"]));
  const server = info.serverUrl || "https://bitwarden.com";
  requireState(info.status === "unlocked" && server.replace(/\/$/, "") === url.replace(/\/$/, "") &&
    (id && secret ? id === `user.${info.userId}` : typeof info.userEmail === "string" && info.userEmail.toLowerCase() === email?.toLowerCase()),
    "Vault identity or server mismatch");
  await command(["sync"]);
  return command;
}

type Entry = Record<string, any>;
const kinds: Record<number, string> = { 1: "login", 2: "secureNote", 3: "card", 4: "identity" };
function inventory(entries: unknown, folders = false): Entry[] {
  requireState(Array.isArray(entries), "Invalid vault inventory");
  // bw list folders includes a synthetic "No Folder" entry with an empty ID.
  const real = folders ? entries.filter(e => !(e?.name === "No Folder" && (e.id === "" || e.id === null))) : entries;
  const ids = new Set<string>();
  for (const e of real) {
    requireState(e && typeof e.id === "string" && e.id && !ids.has(e.id) && typeof e.name === "string" &&
      (folders || !e.decryptionFailure && Number.isInteger(e.type) && (e.fields == null || Array.isArray(e.fields))), "Invalid or duplicate inventory entry");
    ids.add(e.id);
  }
  return real;
}
function supported(e: Entry): boolean {
  // Cross-account copies of attachments, passkeys and SSH keys need a verified round trip.
  return !!kinds[e.type] && !!e[kinds[e.type]] && !e.organizationId && !e.collectionIds?.length &&
    !e.attachments?.length && !e.login?.fido2Credentials?.length && !e.deletedDate && !e.archivedDate && !e.archived;
}
function owned(e: Entry, folderId: string | null, fields: Entry[]) {
  return { name: e.name, type: e.type, notes: e.notes ?? null, favorite: e.favorite ?? false,
    reprompt: e.reprompt ?? 0, folderId, fields, [kinds[e.type]]: e[kinds[e.type]] };
}
function comparable(payload: Entry) {
  // This timestamp is generated by each server and is not a password change.
  if (payload.type !== 1) return payload;
  const { passwordRevisionDate, fido2Credentials, ...login } = payload.login;
  const { username, password, ...other } = login;
  return { ...payload, login: { ...other, username: username || null, password: password || null } };
}
function key(e: Entry) { return JSON.stringify([e.type, e.name, e.type === 1 ? e.login?.username || "" : ""]); }
function plan(src: Entry[], dst: Entry[], srcFolders: Entry[], dstFolders: Entry[]) {
  const folders = new Map<string, string>();
  const targetNames = new Map<string, string | null>();
  for (const folder of dstFolders) {
    const name = folder.name.toLowerCase();
    targetNames.set(name, targetNames.has(name) ? null : folder.id);
  }
  const folderCreates = new Map<string, string>();
  const sourceNames = new Set<string>();
  for (const folder of srcFolders) {
    const name = folder.name.toLowerCase();
    requireState(!sourceNames.has(name), "Ambiguous source folder");
    sourceNames.add(name);
    const target = targetNames.get(name);
    requireState(target !== null, "Ambiguous destination folder");
    if (target) folders.set(folder.id, target);
    else folderCreates.set(folder.id, folder.name);
  }
  // Validate all folders and all ownership claims before creating anything.
  const tracked = new Map<string, Entry>();
  const manual = new Map<string, Entry[]>();
  for (const item of dst) {
    if (item.deletedDate) continue;
    const fields = (item.fields ?? []).filter((f: Entry) => f.name === "bw_id");
    if (fields.length) {
      const id = fields[0].value;
      requireState(fields.length === 1 && typeof id === "string" && id && !tracked.has(id), "Ambiguous destination tracking ID");
      tracked.set(id, item);
    } else if (!item.organizationId) {
      const k = key(item);
      if (!manual.has(k)) manual.set(k, []);
      manual.get(k)!.push(item);
    }
  }
  const sourceCounts = new Map<string, number>();
  const present = new Map(src.map(e => [e.id, e]));
  for (const item of src) if (!tracked.has(item.id) && !item.organizationId) sourceCounts.set(key(item), (sourceCounts.get(key(item)) ?? 0) + 1);
  const writes: { source: Entry; target?: Entry; adopted: boolean; fields: Entry[] }[] = [];
  let skipped = 0;
  for (const item of src) {
    requireState(!item.folderId || folders.has(item.folderId) || folderCreates.has(item.folderId), "Missing source folder");
    if (!supported(item)) { skipped++; continue; }
    let target = tracked.get(item.id);
    let adopted = false;
    if (!target) {
      const matches = manual.get(key(item)) ?? [];
      requireState(matches.length <= 1 && (!matches.length || sourceCounts.get(key(item)) === 1), "Ambiguous manual adoption");
      target = matches[0];
      adopted = !!target;
    }
    if (target && (!supported(target) || target.type !== item.type)) { skipped++; continue; }
    const fields = [...(item.fields ?? []).filter((f: Entry) => f.name !== "bw_id"), { name: "bw_id", value: item.id, type: 0 }];
    if (!target || (item.folderId && !folders.has(item.folderId)) ||
        !isDeepStrictEqual(comparable(owned(item, folders.get(item.folderId) ?? null, fields)), comparable(owned(target, target.folderId ?? null, target.fields ?? [])))) {
      writes.push({ source: item, target, adopted, fields });
    }
  }
  const deletes: { id: string; target: Entry }[] = [];
  for (const [id, target] of tracked) if (!present.has(id)) {
    if (supported(target)) deletes.push({id, target}); else skipped++;
  }
  const eligible = [...tracked].filter(([id, target]) => {
    const source = present.get(id);
    return supported(target) && (!source || (supported(source) && source.type === target.type));
  }).length;
  const blocked = deletes.length > 0 && (!src.some(supported) ||
    deletes.length > Math.min(10, Math.max(1, Math.floor(eligible * 0.05))));
  return { folders, folderCreates, writes, deletes, skipped, blocked };
}

async function main() {
  const args = process.argv.slice(2);
  requireState(args.length === 0 || args.length === 1 && args[0] === "--preview", "Usage: bitwarden-sync [--preview]");
  const { BW_CLIENTID, BW_CLIENTSECRET, BW_PASSWORD, VW_CLIENTID, VW_CLIENTSECRET, VW_EMAIL, VW_PASSWORD } = process.env;
  requireState(BW_CLIENTID && BW_CLIENTSECRET && BW_PASSWORD && VW_PASSWORD && ((VW_CLIENTID && VW_CLIENTSECRET) || VW_EMAIL), "Missing required environment variables");
  const preview = args.length === 1;
  const profile = preview ? await mkdtemp(join(tmpdir(), "bitwarden-preview-")) : state;
  try {
    await synchronize(profile, preview);
  } finally {
    if (preview) await rm(profile, { recursive: true, force: true });
  }
}

async function synchronize(profile: string, preview: boolean) {
  const { BW_CLIENTID, BW_CLIENTSECRET, VW_CLIENTID, VW_CLIENTSECRET, VW_EMAIL, VW_SERVER_URL } = process.env;
  const source = await openVault(join(profile, "src"), VW_SERVER_URL || "https://vault.amahi.dev", "VW_PASSWORD", VW_CLIENTID, VW_CLIENTSECRET, VW_EMAIL);
  const dest = await openVault(join(profile, "dst"), "https://bitwarden.com", "BW_PASSWORD", BW_CLIENTID, BW_CLIENTSECRET);
  async function items(read: typeof source) {
    const active = inventory(JSON.parse(await read(["list", "items"])));
    const archived = inventory(JSON.parse(await read(["list", "items", "--archived"])));
    // The CLI returns the active list again when archive filtering is unavailable.
    const byId = new Map(active.map(item => [item.id, item]));
    for (const item of archived) {
      const previous = byId.get(item.id);
      if (previous) requireState(!item.archivedDate && isDeepStrictEqual(previous, item), "Inconsistent archive inventory");
      else byId.set(item.id, {...item, archived: true});
    }
    // The account-encrypted export enumerates encrypted personal ciphers
    // directly. It includes IDs suppressed from the decrypted or archive list.
    const encrypted = JSON.parse(await read(["export", "--format", "encrypted_json", "--raw"]));
    requireState(encrypted.encrypted === true && Array.isArray(encrypted.items), "Invalid encrypted inventory");
    const personal = new Set<string>();
    for (const row of encrypted.items) {
      requireState(typeof row?.id === "string" && row.id && !personal.has(row.id), "Invalid encrypted inventory ID");
      personal.add(row.id);
      const visible = byId.get(row.id);
      if (visible) {
        requireState(!visible.organizationId && !!row.archivedDate === !!(visible.archived || visible.archivedDate), "Inconsistent personal inventory");
        continue;
      }
      requireState(row.archivedDate && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(row.id),
        "Personal source or destination inventory is incomplete");
      const [hidden] = inventory([JSON.parse(await read(["get", "item", row.id]))]);
      requireState(hidden.id === row.id && !hidden.organizationId && hidden.archivedDate, "Archived identity could not be confirmed");
      byId.set(row.id, {...hidden, archived: true});
    }
    for (const row of byId.values()) requireState(row.organizationId || personal.has(row.id), "Personal inventory changed during preflight");
    return [...byId.values()];
  }
  const changes = plan(await items(source), await items(dest),
    inventory(JSON.parse(await source(["list", "folders"])), true),
    inventory(JSON.parse(await dest(["list", "folders"])), true));
  const created = changes.writes.filter(w => !w.target).length;
  const adopted = changes.writes.filter(w => w.adopted).length;
  console.log(`Plan: ${created} created, ${changes.writes.length-created-adopted} updated, ${adopted} adopted, ${changes.deletes.length} deleted, ${changes.folderCreates.size} source folders to map, ${changes.skipped} skipped.`);
  requireState(!changes.blocked, "Deletion guard held the entire sync; reconcile source and destination before retrying");
  // bw list can omit an undecryptable or archived item. A UUID get uses the
  // encrypted local inventory directly instead of the CLI's text search.
  for (const {id} of changes.deletes) {
    requireState(/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id), "Invalid tracked source UUID");
    try {
      const item = JSON.parse(await source(["get", "item", id]));
      requireState(item.id === id && !item.decryptionFailure && typeof item.deletedDate === "string" &&
        Number.isFinite(Date.parse(item.deletedDate)), "Source absence could not be confirmed");
    } catch (error) {
      if (!(error instanceof MissingItem)) throw error;
    }
  }
  if (preview) return;
  for (const [id, name] of changes.folderCreates) {
    const folder = JSON.parse(await dest(["create", "folder"], { name }));
    requireState(typeof folder.id === "string" && folder.id, "Could not verify created folder");
    changes.folders.set(id, folder.id);
  }
  for (const {source: item, target, fields} of changes.writes) {
    const data = owned(item, changes.folders.get(item.folderId) ?? null, fields);
    if (!target) { await dest(["create", "item"], data); continue; }
    for (let attempt = 0; ; attempt++) {
      try {
        await dest(["edit", "item", target.id], { ...target, ...data });
        break;
      } catch (e) {
        if (!(e instanceof RetryableEdit) || attempt >= 3) throw e;
        console.warn("Transient edit failure; retrying");
        await delay(1000 * 2 ** attempt);
      }
    }
  }
  for (const {target} of changes.deletes) await dest(["delete", "item", target.id]);
  console.log("Sync finished.");
}
if (import.meta.main) main().catch(error => {
  console.error(error instanceof SyncError ? error.message : "Invalid vault response or local failure");
  process.exitCode = 1;
});
