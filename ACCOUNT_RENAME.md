# Account migration: buildfleet to amahi

The login becomes `amahi`, with home `/home/amahi`, UID 1000, primary GID
100, and the original supplementary groups. `/home/buildfleet` remains a
compatibility symlink for existing chats and worktrees. The server hostname,
domains, separate application accounts, and SSH host keys stay unchanged.

The checked deployment comes from the currently deployed flake plus the account
changes. It must not activate unrelated edits from the working checkout.
The isolated `account-rename` flake check rehearses the UNIX/Samba rename,
fresh SSH/sudo and both SMB login names, complete rollback, and recovery after
an injected failure following the home move.

Preflight also rejects applications running under another UID inside the old
login's session cgroup: terminating that login kills those applications too.
The manual Hermes WebUI runs in a transient system unit outside that session;
this does not enable boot autostart. Its HTTPS health check remains mandatory.

## Cutover and reconnect

The detached root controller runs outside the affected user's sessions. It
backs up account/Samba state and edited application configuration under the
root-only `/var/lib/nixos-account-rename/private` directory, pauses competing
maintenance, checkpoints/stops the affected development containers, and stops
the old user runtime before renaming the account and moving the home.

The controller retains the old development containers/images for rollback,
recreates only the previously running projects, and preserves their images,
Git branches, data, and published ports. The previously stopped project stays
stopped. Saved SMB clients may continue using `buildfleet`; `amahi` uses the
same preserved SMB password through the username map.

After the brief SSH/Codex disconnect, update saved client usernames and connect:

```bash
ssh amahi@ssh.amahi.dev
cat /var/lib/nixos-account-rename/status.json
sudo /var/lib/nixos-account-rename/confirm
```

Confirmation requires a fresh `amahi` login and repeats the migration checks.
It cancels the 30-minute rollback watchdog, then commits/pushes only the three
checked TokenEye configuration/recovery files. A push failure leaves the
confirmed account intact; rerunning the confirmation command retries the push.

If verification fails or the watchdog expires before confirmation, the
controller restores the original account, home path, Samba credential database,
edited configuration, previous generation, and original running containers.
Reconnect using `buildfleet` after a rollback. The root controller log is:

```bash
sudo journalctl -u account-rename.service -u account-rename-rollback.service
```

Manual rollback before confirmation is available with:

```bash
sudo /var/lib/nixos-account-rename/rollback
```

Backups and the home compatibility link are retained after success. They are
not deleted automatically. Private credentials and password fingerprints must
never be copied into Git or application logs.

## Existing service limitation

The November 4 cutover rolled back after a retained `agent-config` container
reused ports 32828–32832 instead of the captured 32848–32852. Native recreation
now uses `devcontainer up --remove-existing-container` for the new Compose
identity only; original containers remain available for rollback. A regression
test checks that retrying replaces stale port mappings.

Do not reuse the November 4 prepared target or backups for a new cutover.
Archive the rolled-back controller state, build from the currently deployed
flake plus the current migration changes, and take fresh backups. The candidate
must retain Cloudflare and DuckDNS updates, newer TokenEye update units, shopping
service ownership, and current maintenance protections. The existing Nextcloud
maintenance flag is captured and checked rather than assumed.
