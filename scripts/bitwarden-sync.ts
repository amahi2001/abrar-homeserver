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

  if (stdinValue) {
    const child = command.spawn();
    const writer = child.stdin.getWriter();
    await writer.write(new TextEncoder().encode(stdinValue));
    await writer.close();
    const { code, stdout, stderr } = await child.output();
    const outStr = new TextDecoder().decode(stdout).trim();
    const errStr = new TextDecoder().decode(stderr).trim();
    if (code !== 0) {
      throw new Error(`Command ${cmd} ${args.join(" ")} failed with code ${code}.\nError: ${errStr}\nOutput: ${outStr}`);
    }
    return outStr;
  } else {
    const { code, stdout, stderr } = await command.output();
    const outStr = new TextDecoder().decode(stdout).trim();
    const errStr = new TextDecoder().decode(stderr).trim();
    if (code !== 0) {
      throw new Error(`Command ${cmd} ${args.join(" ")} failed with code ${code}.\nError: ${errStr}\nOutput: ${outStr}`);
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

function normalizeForComparison(item: any) {
  const normalized = JSON.parse(JSON.stringify(item));
  delete normalized.id;
  delete normalized.folderId;
  delete normalized.revisionDate;
  delete normalized.creationDate;
  delete normalized.collectionIds;
  delete normalized.organizationId;
  
  if (normalized.fields) {
    normalized.fields = normalized.fields
      .map((f: any) => ({ name: f.name, value: f.value, type: f.type }))
      .sort((a: any, b: any) => (a.name || "").localeCompare(b.name || ""));
  }
  
  if (normalized.login && normalized.login.uris) {
    normalized.login.uris = normalized.login.uris
      .map((u: any) => ({ uri: u.uri, match: u.match }))
      .sort((a: any, b: any) => (a.uri || "").localeCompare(b.uri || ""));
  }
  
  return normalized;
}

function prepareTargetItem(srcItem: any, targetId: string | null, targetFolderId: string | null) {
  const item = JSON.parse(JSON.stringify(srcItem));
  item.id = targetId;
  item.folderId = targetFolderId;
  
  delete item.revisionDate;
  item.creationDate = null;
  
  if (!item.fields) {
    item.fields = [];
  }
  item.fields = item.fields.filter((f: any) => f.name !== "bw_id");
  item.fields.push({
    name: "bw_id",
    value: srcItem.id,
    type: 0 // Text
  });
  
  return item;
}

async function main() {
  console.log(`[${new Date().toISOString()}] Starting Bitwarden -> Vaultwarden Sync...`);

  // Ensure directories exist
  await Deno.mkdir(SRC_DIR, { recursive: true });
  await Deno.mkdir(DST_DIR, { recursive: true });

  // Get credentials
  const BW_CLIENTID = Deno.env.get("BW_CLIENTID");
  const BW_CLIENTSECRET = Deno.env.get("BW_CLIENTSECRET");
  const BW_PASSWORD = Deno.env.get("BW_PASSWORD");
  const VW_EMAIL = Deno.env.get("VW_EMAIL");
  const VW_PASSWORD = Deno.env.get("VW_PASSWORD");
  const VW_SERVER_URL = Deno.env.get("VW_SERVER_URL") || "https://buildfleet.duckdns.org/vault";

  if (!BW_CLIENTID || !BW_CLIENTSECRET || !BW_PASSWORD || !VW_EMAIL || !VW_PASSWORD) {
    console.error("Missing required environment variables!");
    Deno.exit(1);
  }

  // Configure servers
  console.log("Configuring servers...");
  await runBw(SRC_DIR, ["config", "server", "https://api.bitwarden.com"]);
  await runBw(DST_DIR, ["config", "server", VW_SERVER_URL]);

  // Unlock source (Bitwarden)
  console.log("Logging into source (Bitwarden)...");
  try {
    await runBw(SRC_DIR, ["login", "--apikey"], undefined, { BW_CLIENTID, BW_CLIENTSECRET });
  } catch (e) {
    // Already logged in or other non-fatal login issue
  }

  console.log("Unlocking source...");
  const srcSessionRaw = await runBw(SRC_DIR, ["unlock", "--passwordenv", "BW_PASSWORD"], undefined, { BW_PASSWORD });
  // Extract session token (usually follows 'export BW_SESSION="TOKEN"')
  const srcSessionMatch = srcSessionRaw.match(/export BW_SESSION="([^"]+)"/);
  if (!srcSessionMatch) {
    throw new Error("Failed to extract source session token");
  }
  const srcSession = srcSessionMatch[1];

  console.log("Syncing source database...");
  await runBw(SRC_DIR, ["sync", "--session", srcSession]);

  // Unlock target (Vaultwarden)
  console.log("Logging into target (Vaultwarden)...");
  try {
    await runBw(DST_DIR, ["login", VW_EMAIL, "--passwordenv", "VW_PASSWORD"], undefined, { VW_PASSWORD });
  } catch (e) {
    // Already logged in
  }

  console.log("Unlocking target...");
  const dstSessionRaw = await runBw(DST_DIR, ["unlock", "--passwordenv", "VW_PASSWORD"], undefined, { VW_PASSWORD });
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
      const createdRaw = await runBw(DST_DIR, ["create", "folder", "--session", dstSession], JSON.stringify({ name: srcFolder.name }));
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

  for (const srcItem of srcItems) {
    const existingDstItem = dstBwIdMap[srcItem.id];
    const targetFolderId = srcItem.folderId ? folderIdMap[srcItem.folderId] || null : null;
    const preparedItem = prepareTargetItem(srcItem, existingDstItem?.id || null, targetFolderId);

    if (!existingDstItem) {
      // Create new item
      console.log(`Creating item [${srcItem.name}] (${srcItem.id}) in target...`);
      await runBw(DST_DIR, ["create", "item", "--session", dstSession], JSON.stringify(preparedItem));
      createdCount++;
    } else {
      // Check if we need to update
      const normPrepared = normalizeForComparison(preparedItem);
      const normExisting = normalizeForComparison(existingDstItem);

      if (JSON.stringify(normPrepared) !== JSON.stringify(normExisting)) {
        console.log(`Updating item [${srcItem.name}] (${existingDstItem.id}) in target...`);
        await runBw(DST_DIR, ["edit", "item", existingDstItem.id, "--session", dstSession], JSON.stringify(preparedItem));
        updatedCount++;
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

  console.log(`[${new Date().toISOString()}] Sync finished: ${createdCount} created, ${updatedCount} updated, ${deletedCount} deleted.`);
}

if (import.meta.main) {
  try {
    await main();
  } catch (err) {
    console.error("Fatal sync error:", err);
    Deno.exit(1);
  }
}
