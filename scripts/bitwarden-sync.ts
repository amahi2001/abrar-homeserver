#!/usr/bin/env dx
// Bitwarden to Vaultwarden synchronization script

import { existsSync } from "https://deno.land/std@0.224.0/fs/exists.ts";

const SRC_DIR = "/var/lib/bitwarden-sync/src";
const DST_DIR = "/var/lib/bitwarden-sync/dst";

// Helper to run a command
async function runCommand(cmd: string, args: string[], stdinValue?: string, env: Record<string, string> = {}): Promise<string> {
  const command = new Deno.Command(cmd, {
    args,
    stdin: stdinValue ? "piped" : "inherit",
    stdout: "piped",
    stderr: "piped",
    env: {
      ...Deno.env.toObject(),
      ...env,
    },
  });

  const displayArgs = args
    .map((arg, index) => args[index - 1] === "--session" ? "[REDACTED]" : arg)
    .join(" ");

  if (stdinValue) {
    const child = command.spawn();
    const writer = child.stdin.getWriter();
    await writer.write(new TextEncoder().encode(stdinValue));
    await writer.close();
    const { code, stdout, stderr } = await child.output();
    const outStr = new TextDecoder().decode(stdout).trim();
    const errStr = new TextDecoder().decode(stderr).trim();
    if (code !== 0) {
      throw new Error(`Command ${cmd} ${displayArgs} failed with code ${code}.\nError: ${errStr}\nOutput: ${outStr}`);
    }
    return outStr;
  } else {
    const { code, stdout, stderr } = await command.output();
    const outStr = new TextDecoder().decode(stdout).trim();
    const errStr = new TextDecoder().decode(stderr).trim();
    if (code !== 0) {
      throw new Error(`Command ${cmd} ${displayArgs} failed with code ${code}.\nError: ${errStr}\nOutput: ${outStr}`);
    }
    return outStr;
  }
}

async function runBw(appDataDir: string, args: string[], stdinValue?: string, env: Record<string, string> = {}): Promise<string> {
  return await runCommand("bw", args, stdinValue, {
    BITWARDENCLI_APPDATA_DIR: appDataDir,
    ...env,
  });
}

function isTransientBitwardenError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /FetchError|network|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|socket hang up|\b(?:429|500|502|503|504)\b/i.test(message);
}

async function editItemWithRetry(appDataDir: string, itemId: string, session: string, encodedItem: string): Promise<void> {
  const maxAttempts = 4;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      await runBw(appDataDir, ["edit", "item", itemId, "--session", session], encodedItem);
      return;
    } catch (error) {
      if (!isTransientBitwardenError(error) || attempt === maxAttempts) {
        throw error;
      }
      const delayMs = 1000 * 2 ** (attempt - 1);
      console.warn(`Transient Bitwarden API failure updating ${itemId}; retrying in ${delayMs}ms (attempt ${attempt + 1}/${maxAttempts}).`);
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
}

// `bw config server` refuses to run while the profile is still logged in,
// even when it is locked and the configured URL has not changed. The sync
// service is non-interactive and reuses persistent app-data directories, so
// make this setup step idempotent by logging out before reconfiguring.
async function configureBwServer(appDataDir: string, serverUrl: string): Promise<void> {
  try {
    await runBw(appDataDir, ["logout"]);
  } catch (_) {
    // No active login is fine; continue with server configuration.
  }
  await runBw(appDataDir, ["config", "server", serverUrl]);
}

function encodeBase64(str: string): string {
  const bytes = new TextEncoder().encode(str);
  let binary = "";
  const len = bytes.byteLength;
  for (let i = 0; i < len; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

function normalizeForComparison(item: any) {
  // Compare only fields this synchronizer owns. Bitwarden adds server metadata
  // (object, timestamps, password history, attachments) to fetched items; that
  // metadata must not turn a no-op sync into hundreds of edits.
  const normalizeFields = (fields: any[] = []) => fields
    .map((field: any) => ({ name: field.name || "", value: field.value ?? null, type: field.type ?? 0 }))
    .sort((a: any, b: any) => a.name.localeCompare(b.name));
  const normalizeUris = (uris: any[] = []) => uris
    .map((uri: any) => ({ uri: uri.uri || "", match: uri.match ?? null }))
    .sort((a: any, b: any) => a.uri.localeCompare(b.uri));

  const normalized: any = {
    name: item.name || "",
    type: item.type,
    notes: item.notes || null,
    favorite: item.favorite || false,
    reprompt: item.reprompt || 0,
    folderId: item.folderId || null,
    fields: normalizeFields(item.fields),
  };

  if (item.type === 1 && item.login) {
    normalized.login = {
      username: item.login.username || null,
      password: item.login.password || null,
      totp: item.login.totp || null,
      uris: normalizeUris(item.login.uris),
    };
  } else if (item.type === 2 && item.secureNote) {
    normalized.secureNote = { type: item.secureNote.type || 0 };
  } else if (item.type === 3 && item.card) {
    normalized.card = {
      cardholderName: item.card.cardholderName || null,
      brand: item.card.brand || null,
      number: item.card.number || null,
      expMonth: item.card.expMonth || null,
      expYear: item.card.expYear || null,
      code: item.card.code || null,
    };
  } else if (item.type === 4 && item.identity) {
    normalized.identity = {
      title: item.identity.title || null,
      firstName: item.identity.firstName || null,
      middleName: item.identity.middleName || null,
      lastName: item.identity.lastName || null,
      address1: item.identity.address1 || null,
      address2: item.identity.address2 || null,
      address3: item.identity.address3 || null,
      city: item.identity.city || null,
      state: item.identity.state || null,
      postalCode: item.identity.postalCode || null,
      country: item.identity.country || null,
      company: item.identity.company || null,
      email: item.identity.email || null,
      phone: item.identity.phone || null,
      ssn: item.identity.ssn || null,
      username: item.identity.username || null,
    };
  }

  return normalized;
}

function cleanItemForBw(item: any, targetId: string | null, targetFolderId: string | null, srcItemId: string) {
  const cleaned: any = {
    name: item.name || "",
    type: item.type,
    notes: item.notes || null,
    favorite: item.favorite || false,
    reprompt: item.reprompt || 0,
    fields: item.fields || [],
  };

  if (targetId) {
    cleaned.id = targetId;
  }
  if (targetFolderId) {
    cleaned.folderId = targetFolderId;
  }

  // Ensure the custom field bw_id is present and has the correct source item ID
  cleaned.fields = (cleaned.fields || []).filter((f: any) => f.name !== "bw_id");
  cleaned.fields.push({
    name: "bw_id",
    value: srcItemId,
    type: 0 // Text
  });

  if (item.type === 1 && item.login) {
    cleaned.login = {
      username: item.login.username || null,
      password: item.login.password || null,
      totp: item.login.totp || null,
      uris: (item.login.uris || []).map((u: any) => ({ uri: u.uri || "", match: u.match ?? null })),
    };
  } else if (item.type === 2 && item.secureNote) {
    cleaned.secureNote = {
      type: item.secureNote.type || 0,
    };
  } else if (item.type === 3 && item.card) {
    cleaned.card = {
      cardholderName: item.card.cardholderName || null,
      brand: item.card.brand || null,
      number: item.card.number || null,
      expMonth: item.card.expMonth || null,
      expYear: item.card.expYear || null,
      code: item.card.code || null,
    };
  } else if (item.type === 4 && item.identity) {
    cleaned.identity = {
      title: item.identity.title || null,
      firstName: item.identity.firstName || null,
      middleName: item.identity.middleName || null,
      lastName: item.identity.lastName || null,
      address1: item.identity.address1 || null,
      address2: item.identity.address2 || null,
      address3: item.identity.address3 || null,
      city: item.identity.city || null,
      state: item.identity.state || null,
      postalCode: item.identity.postalCode || null,
      country: item.identity.country || null,
      company: item.identity.company || null,
      email: item.identity.email || null,
      phone: item.identity.phone || null,
      ssn: item.identity.ssn || null,
      username: item.identity.username || null,
    };
  }

  return cleaned;
}

function findMatchingDstItem(srcItem: any, dstItems: any[], dstBwIdMap: Record<string, any>) {
  // First, check by tracking custom field
  const byBwId = dstBwIdMap[srcItem.id];
  if (byBwId) {
    return byBwId;
  }

  // Next, check by name, type, and login username for adoption (first sync match)
  const matches = dstItems.filter((di: any) => {
    // If the target item already has a bw_id mapped to something else, don't match it
    const bwIdField = di.fields?.find((f: any) => f.name === "bw_id");
    if (bwIdField && bwIdField.value) {
      return false;
    }

    if (di.name !== srcItem.name || di.type !== srcItem.type) {
      return false;
    }

    if (srcItem.type === 1 && di.login && srcItem.login) {
      return (di.login.username || "") === (srcItem.login.username || "");
    }

    return true;
  });

  return matches.length > 0 ? matches[0] : null;
}

async function main() {
  console.log(`[${new Date().toISOString()}] Starting Vaultwarden -> Bitwarden Sync...`);

  // Ensure directories exist
  await Deno.mkdir(SRC_DIR, { recursive: true });
  await Deno.mkdir(DST_DIR, { recursive: true });

  // Get credentials
  const BW_CLIENTID = Deno.env.get("BW_CLIENTID");
  const BW_CLIENTSECRET = Deno.env.get("BW_CLIENTSECRET");
  const BW_PASSWORD = Deno.env.get("BW_PASSWORD");
  const VW_CLIENTID = Deno.env.get("VW_CLIENTID");
  const VW_CLIENTSECRET = Deno.env.get("VW_CLIENTSECRET");
  const VW_EMAIL = Deno.env.get("VW_EMAIL");
  const VW_PASSWORD = Deno.env.get("VW_PASSWORD");
  const VW_SERVER_URL = Deno.env.get("VW_SERVER_URL") || "https://buildfleet.duckdns.org/vault";

  if (!BW_CLIENTID || !BW_CLIENTSECRET || !BW_PASSWORD || !VW_PASSWORD || (!VW_CLIENTID && !VW_EMAIL)) {
    console.error("Missing required environment variables!");
    Deno.exit(1);
  }

  // Configure servers
  console.log("Configuring servers...");
  // Source is local Vaultwarden
  await configureBwServer(SRC_DIR, VW_SERVER_URL);
  // Target is cloud Bitwarden
  await configureBwServer(DST_DIR, "https://bitwarden.com");

  // Unlock source (Vaultwarden)
  console.log("Logging into source (Vaultwarden)...");
  try {
    if (VW_CLIENTID && VW_CLIENTSECRET) {
      await runBw(SRC_DIR, ["login", "--apikey"], undefined, { BW_CLIENTID: VW_CLIENTID, BW_CLIENTSECRET: VW_CLIENTSECRET });
    } else {
      await runBw(SRC_DIR, ["login", VW_EMAIL!, "--passwordenv", "VW_PASSWORD"], undefined, { VW_PASSWORD });
    }
  } catch (e) {
    // Already logged in or other non-fatal login issue
  }

  console.log("Unlocking source...");
  const srcSessionRaw = await runBw(SRC_DIR, ["unlock", "--passwordenv", "VW_PASSWORD"], undefined, { VW_PASSWORD });
  const srcSessionMatch = srcSessionRaw.match(/export BW_SESSION="([^"]+)"/);
  if (!srcSessionMatch) {
    throw new Error("Failed to extract source session token");
  }
  const srcSession = srcSessionMatch[1];

  console.log("Syncing source database...");
  await runBw(SRC_DIR, ["sync", "--session", srcSession]);

  // Unlock target (Bitwarden)
  console.log("Logging into target (Bitwarden)...");
  try {
    await runBw(DST_DIR, ["login", "--apikey"], undefined, { BW_CLIENTID, BW_CLIENTSECRET });
  } catch (e) {
    // Already logged in
  }

  console.log("Unlocking target...");
  const dstSessionRaw = await runBw(DST_DIR, ["unlock", "--passwordenv", "BW_PASSWORD"], undefined, { BW_PASSWORD });
  const dstSessionMatch = dstSessionRaw.match(/export BW_SESSION="([^"]+)"/);
  if (!dstSessionMatch) {
    throw new Error("Failed to extract target session token");
  }
  const dstSession = dstSessionMatch[1];

  console.log("Syncing target database...");
  await runBw(DST_DIR, ["sync", "--session", dstSession]);

  // Fetch items and folders
  console.log("Fetching items and folders...");
  const srcItems = JSON.parse(await runBw(SRC_DIR, ["list", "items", "--session", srcSession]));
  const srcFolders = JSON.parse(await runBw(SRC_DIR, ["list", "folders", "--session", srcSession]));
  const dstItems = JSON.parse(await runBw(DST_DIR, ["list", "items", "--session", dstSession]));
  const dstFolders = JSON.parse(await runBw(DST_DIR, ["list", "folders", "--session", dstSession]));

  console.log(`Source: ${srcItems.length} items, ${srcFolders.length} folders`);
  console.log(`Target: ${dstItems.length} items, ${dstFolders.length} folders`);

  // Map Folders
  const folderIdMap: Record<string, string> = {};
  for (const srcFolder of srcFolders) {
    const matchingDst = dstFolders.find((df: any) => df.name.toLowerCase() === srcFolder.name.toLowerCase());
    if (matchingDst) {
      folderIdMap[srcFolder.id] = matchingDst.id;
    } else {
      console.log(`Creating folder '${srcFolder.name}' in target...`);
      const b64Data = encodeBase64(JSON.stringify({ name: srcFolder.name }));
      const createdRaw = await runBw(DST_DIR, ["create", "folder", "--session", dstSession], b64Data);
      const created = JSON.parse(createdRaw);
      folderIdMap[srcFolder.id] = created.id;
    }
  }

  // Create target tracking lookup by bw_id custom field
  const dstBwIdMap: Record<string, any> = {};
  for (const item of dstItems) {
    const bwIdField = item.fields?.find((f: any) => f.name === "bw_id");
    if (bwIdField && bwIdField.value) {
      dstBwIdMap[bwIdField.value] = item;
    }
  }

  // Sync Items
  console.log("Syncing items...");
  let createdCount = 0;
  let updatedCount = 0;
  let adoptedCount = 0;

  for (const srcItem of srcItems) {
    const existingDstItem = findMatchingDstItem(srcItem, dstItems, dstBwIdMap);
    const targetFolderId = srcItem.folderId ? folderIdMap[srcItem.folderId] || null : null;
    const preparedItem = cleanItemForBw(srcItem, existingDstItem?.id || null, targetFolderId, srcItem.id);

    if (!existingDstItem) {
      // Create new item
      console.log(`Creating item [${srcItem.name}] (${srcItem.id}) in target...`);
      const b64Data = encodeBase64(JSON.stringify(preparedItem));
      await runBw(DST_DIR, ["create", "item", "--session", dstSession], b64Data);
      createdCount++;
    } else {
      // Check if we need to update
      const normPrepared = normalizeForComparison(preparedItem);
      const normExisting = normalizeForComparison(existingDstItem);

      const dstHasBwId = existingDstItem.fields?.some((f: any) => f.name === "bw_id");

      if (JSON.stringify(normPrepared) !== JSON.stringify(normExisting)) {
        if (!dstHasBwId) {
          console.log(`Adopting and updating manually imported item [${srcItem.name}] (${existingDstItem.id}) with tracking ID...`);
          adoptedCount++;
        } else {
          console.log(`Updating item [${srcItem.name}] (${existingDstItem.id}) in target...`);
          updatedCount++;
        }
        const b64Data = encodeBase64(JSON.stringify(preparedItem));
        await editItemWithRetry(DST_DIR, existingDstItem.id, dstSession, b64Data);
      } else if (!dstHasBwId) {
        console.log(`Adopting manually imported item [${srcItem.name}] (${existingDstItem.id}) with tracking ID...`);
        const b64Data = encodeBase64(JSON.stringify(preparedItem));
        await editItemWithRetry(DST_DIR, existingDstItem.id, dstSession, b64Data);
        adoptedCount++;
      }
    }
  }

  // Delete items in target that are no longer in source
  let deletedCount = 0;
  const srcItemIdSet = new Set(srcItems.map((item: any) => item.id));
  for (const item of dstItems) {
    const bwIdField = item.fields?.find((f: any) => f.name === "bw_id");
    if (bwIdField && bwIdField.value && !srcItemIdSet.has(bwIdField.value)) {
      console.log(`Deleting item [${item.name}] (${item.id}) from target since it was deleted in source...`);
      await runBw(DST_DIR, ["delete", "item", item.id, "--session", dstSession]);
      deletedCount++;
    }
  }

  console.log(`[${new Date().toISOString()}] Sync finished: ${createdCount} created, ${updatedCount} updated, ${adoptedCount} adopted, ${deletedCount} deleted.`);
}

if (import.meta.main) {
  try {
    await main();
  } catch (err) {
    console.error("Fatal sync error:", err);
    Deno.exit(1);
  }
}
