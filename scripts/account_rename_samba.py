#!/usr/bin/env python3
"""Preserve the local Samba identity during the buildfleet/amahi migration.

Snapshots contain a password-derived fingerprint. Keep them in the controller's
root-only backup directory; neither this module nor its CLI logs that value.
"""

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile


NAMES = frozenset(("buildfleet", "amahi"))
INVARIANTS = (
    "user_sid", "group_sid", "acct_ctrl", "credential_fingerprint",
    "pass_last_set_time", "pass_can_change_time", "pass_must_change_time",
)


def _require_root():
    if os.geteuid() != 0:
        raise PermissionError("Samba account migration requires root")


def _modules():
    """Use the installed Samba Python modules, including Nix dependencies."""
    try:
        from samba.samba3 import param, passdb
        return param, passdb
    except ImportError:
        executable = shutil.which("smbd")
        if not executable:
            raise RuntimeError("Installed Samba executable not found") from None
        package = Path(executable).resolve().parent.parent
        roots = [package]
        if package.parent == Path("/nix/store"):
            result = subprocess.run(
                ["nix-store", "--query", "--references", str(package)],
                check=True, capture_output=True, text=True,
            )
            roots.extend(Path(line) for line in result.stdout.splitlines())
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        for root in roots:
            directory = root / "lib" / version / "site-packages"
            if directory.is_dir():
                sys.path.insert(0, str(directory))
        # A script under /etc can initially resolve /etc/samba as a namespace
        # package. Replace only that failed namespace import, not real Samba.
        cached = sys.modules.get("samba")
        if cached is not None and getattr(cached, "__file__", None) is None:
            for name in tuple(sys.modules):
                if name == "samba" or name.startswith("samba."):
                    del sys.modules[name]
        from samba.samba3 import param, passdb
        return param, passdb


def _open(config_path, backend=None, rename_script=None):
    _require_root()
    config_path = Path(config_path)
    if not config_path.is_file():
        raise ValueError("Samba configuration file is missing")
    param, passdb = _modules()
    passdb.set_smb_config(str(config_path))
    context = param.get_context()
    context.load(str(config_path))
    backend = backend or context.get("passdb backend")
    if not isinstance(backend, str) or not (
        backend == "tdbsam" or backend.startswith("tdbsam:")
    ):
        raise ValueError("Only the existing local tdbsam backend is supported")
    # This override is process-local; no persistent account-management hook.
    context.set("rename user script", rename_script or "")
    return passdb.PDB(backend), passdb.error


def _name(username):
    if username not in NAMES:
        raise ValueError("Only buildfleet and amahi are valid migration accounts")


def _account(database, error_type, username):
    try:
        return database.getsampwnam(username)
    except error_type as error:
        # Do not treat database errors as an absent account.
        message = str(error)
        if any(status in message for status in
               ("3221225572", "-1073741724", "NT_STATUS_NO_SUCH_USER")):
            return None
        raise RuntimeError("Cannot read Samba account metadata") from None


def _snapshot(account):
    digest = hashlib.sha256()
    for field in ("nt_passwd", "lanman_passwd", "pw_history"):
        value = getattr(account, field)
        if value is not None and not isinstance(value, bytes):
            value = bytes(value)
        digest.update(field.encode() + b"\0")
        digest.update(b"none" if value is None else len(value).to_bytes(8, "big") + value)
    return {
        "username": account.username,
        "user_sid": str(account.user_sid),
        "group_sid": str(account.group_sid),
        "acct_ctrl": int(account.acct_ctrl),
        "credential_fingerprint": digest.hexdigest(),
        "pass_last_set_time": int(account.pass_last_set_time),
        "pass_can_change_time": int(account.pass_can_change_time),
        "pass_must_change_time": int(account.pass_must_change_time),
    }


def preserved(expected, actual):
    """Compare credentials internally; return no password-derived details."""
    return all(
        hmac.compare_digest(str(expected.get(field, "")), str(actual.get(field, "")))
        and field in expected and field in actual
        for field in INVARIANTS
    )


def snapshot(config_path="/etc/samba/smb.conf", username="buildfleet", backend=None):
    _name(username)
    database, error_type = _open(config_path, backend)
    account = _account(database, error_type, username)
    if account is None:
        raise ValueError("Expected Samba account does not exist")
    return _snapshot(account)


def verify(config_path, expected, username="amahi", backend=None):
    actual = snapshot(config_path, username, backend)
    if not preserved(expected, actual):
        raise RuntimeError("Samba password, SID or account flags changed")
    return actual


def verify_logins(config_path, expected, private_directory, username="amahi", backend=None):
    """Authenticate both SMB names using an ephemeral private NT-hash file.

    The hash never enters command arguments, subprocess output or the return
    value. This performs read-only tree connections to the local Media share.
    """
    _require_root()
    _name(username)
    directory = Path(private_directory)
    info = directory.stat()
    if directory.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("SMB verification requires a private root-owned directory")
    executable = shutil.which("smbclient")
    if not executable:
        raise RuntimeError("Installed smbclient executable not found")
    database, error_type = _open(config_path, backend)
    account = _account(database, error_type, username)
    if account is None or not preserved(expected, _snapshot(account)):
        raise RuntimeError("Samba identity differs from the migration snapshot")
    password = account.nt_passwd
    if not isinstance(password, bytes) or len(password) != 16:
        raise RuntimeError("Samba NT credential is unavailable for authentication verification")
    descriptor, filename = tempfile.mkstemp(prefix="smb-verify-", dir=directory)
    try:
        os.close(descriptor)
        for login in ("buildfleet", "amahi"):
            with open(filename, "w") as stream:
                stream.write(f"username = {login}\npassword = {password.hex()}\n")
            result = subprocess.run(
                [executable, "//127.0.0.1/Media", "-I", "127.0.0.1",
                 "--configfile", str(config_path), "--use-kerberos=off", "--pw-nt-hash",
                 "-A", filename, "-t", "10", "-c", "pwd"],
                capture_output=True, timeout=20,
            )
            if result.returncode:
                raise RuntimeError(f"SMB authentication verification failed for {login}")
    finally:
        Path(filename).unlink(missing_ok=True)
    return {"buildfleet": True, "amahi": True}


def _validate_script(command):
    arguments = shlex.split(command)
    if not arguments or not re.fullmatch(r"/[A-Za-z0-9_./-]+", arguments[0]):
        raise ValueError("The rename callback must be an absolute root-owned executable")
    if arguments[1:] not in ([], ["%uold", "%unew"]):
        raise ValueError("The rename callback only accepts the old/new placeholders")
    path = Path(arguments[0]).resolve(strict=True)
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
        raise ValueError("The rename callback must be a root-owned protected regular file")
    if not info.st_mode & 0o111:
        raise ValueError("The rename callback is not executable")


def rename(config_path="/etc/samba/smb.conf", old="buildfleet", new="amahi",
           rename_script=None, backend=None, expected=None):
    """Rename through tdbsam, preserving credential/SID data and handling retries.

The callback must either perform the coordinated UNIX rename or return success
after the controller already completed it. A failed callback/database operation
can leave UNIX state changed; the controller owns the overall rollback.
"""
    _require_root()
    _name(old)
    _name(new)
    if old == new:
        raise ValueError("The old and new names must differ")
    if not rename_script:
        raise ValueError("A protected UNIX-rename callback is required")
    _validate_script(rename_script)
    database, error_type = _open(config_path, backend, rename_script)
    source = _account(database, error_type, old)
    destination = _account(database, error_type, new)
    if source is None:
        if destination is not None and expected is not None:
            actual = _snapshot(destination)
            if preserved(expected, actual):
                return actual
        raise RuntimeError("Missing source account or unverified partial Samba migration")
    if destination is not None:
        raise RuntimeError("Destination Samba account already exists")
    before = _snapshot(source)
    if expected is not None and not preserved(expected, before):
        raise RuntimeError("Samba account differs from the saved migration snapshot")
    try:
        database.rename_sam_account(source, new)
    except error_type:
        raise RuntimeError("Samba account rename failed; controller rollback is required") from None
    after = _account(database, error_type, new)
    if after is None or _account(database, error_type, old) is not None:
        raise RuntimeError("Samba account rename did not complete")
    actual = _snapshot(after)
    if not preserved(before, actual):
        raise RuntimeError("Samba password, SID or account flags changed")
    return actual


def _read_snapshot(path):
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o077 or path.is_symlink():
        raise ValueError("The saved Samba snapshot must be a private root-owned file")
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "rename", "verify"))
    parser.add_argument("--config", default="/etc/samba/smb.conf")
    parser.add_argument("--backend")
    parser.add_argument("--old", default="buildfleet")
    parser.add_argument("--new", default="amahi")
    parser.add_argument("--rename-script")
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    _require_root()
    if args.action == "snapshot":
        value = snapshot(args.config, args.old, args.backend)
        descriptor = os.open(args.snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.write("\n")
    else:
        expected = _read_snapshot(args.snapshot)
        if args.action == "rename":
            rename(args.config, args.old, args.new, args.rename_script, args.backend, expected)
        else:
            verify(args.config, expected, args.new, args.backend)
    print("Samba migration check passed")


if __name__ == "__main__":
    main()
