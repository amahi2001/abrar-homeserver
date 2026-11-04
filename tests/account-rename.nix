{ pkgs, lib, ... }:
let
  old = "buildfleet";
  new = "amahi";
  callback = pkgs.writeShellScript "rehearsal-rename-user" ''
    set -eu
    test "$#" = 2
    test "$1" = ${old}
    test "$2" = ${new}
    ${pkgs.shadow}/bin/usermod --login "$2" --home "/home/$2" --move-home "$1"
    if test -e /root/inject-rename-failure; then exit 1; fi
  '';
  rehearsal = pkgs.writeText "account-rename-rehearsal.py" ''
    import json
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    import time

    os.environ["PATH"] = "/run/current-system/sw/bin:/run/wrappers/bin"
    sys.path.insert(0, "/etc")
    import account_rename_samba as samba
    sys.path[:] = [entry for entry in sys.path if entry != "/etc"]

    OLD, NEW = "${old}", "${new}"
    BEFORE, AFTER = Path("/home/" + OLD), Path("/home/" + NEW)
    BACKUP = Path("/root/account-rename-backup")
    ACCOUNT_FILES = ("passwd", "shadow", "group", "gshadow", "subuid", "subgid")

    def run(*args):
        result = subprocess.run(args, text=True, capture_output=True)
        if result.returncode:
            print(result.stdout, result.stderr, file=sys.stderr)
        result.check_returncode()
        return result.stdout.strip()

    def account_line(file, user):
        return next(line.split(":")[1:] for line in Path("/etc", file).read_text().splitlines()
                    if line.startswith(user + ":"))

    def quiesce(user):
        subprocess.run(["loginctl", "disable-linger", user], capture_output=True)
        subprocess.run(["loginctl", "terminate-user", user], capture_output=True)
        run("systemctl", "stop", "user@1000.service", "user-runtime-dir@1000.service")
        for attempt in range(100):
            if subprocess.run(["loginctl", "show-user", "1000"], capture_output=True).returncode:
                return
            time.sleep(0.1)
        raise RuntimeError("The previous logind user record has not stopped")

    def prepare():
        BACKUP.mkdir(mode=0o700)
        for file in ACCOUNT_FILES:
            if Path("/etc", file).exists():
                shutil.copy2(Path("/etc", file), BACKUP / file)
        run("systemctl", "stop", "samba-smbd.service")
        shutil.copy2("/var/lib/samba/private/passdb.tdb", BACKUP / "passdb.tdb")
        baseline = {
            "shadow": account_line("shadow", OLD),
            "samba": samba.snapshot("/etc/samba/smb.conf", OLD),
            "home_inode": BEFORE.stat().st_ino,
            "key": (BEFORE / ".ssh/authorized_keys").read_text(),
            "system": run("readlink", "-f", "/run/current-system"),
        }
        (BACKUP / "baseline.json").write_text(json.dumps(baseline))
        run("systemctl", "start", "samba-smbd.service")

    def forward():
        baseline = json.loads((BACKUP / "baseline.json").read_text())
        assert not AFTER.exists()
        run("systemctl", "stop", "samba-smbd.service")
        quiesce(OLD)
        samba.rename("/etc/samba/smb.conf", OLD, NEW,
                     rename_script="${callback} %uold %unew", expected=baseline["samba"])
        BEFORE.symlink_to(AFTER, target_is_directory=True)
        for file in ("subuid", "subgid"):
            path = Path("/etc", file)
            path.write_text("\n".join(NEW + line[len(OLD):] if line.startswith(OLD + ":") else line
                                      for line in path.read_text().splitlines()) + "\n")
        Path("/run/account-user-map").write_text("!amahi = buildfleet\n")
        run("loginctl", "enable-linger", NEW)
        run("systemctl", "start", "user@1000.service")
        run("systemctl", "start", "samba-smbd.service")

    def verify():
        baseline = json.loads((BACKUP / "baseline.json").read_text())
        assert run("id", "-u", NEW) == "1000"
        assert run("id", "-g", NEW) == "100"
        assert "wheel" in run("id", "-nG", NEW).split()
        assert account_line("shadow", NEW) == baseline["shadow"]
        assert account_line("subuid", NEW) == ["100000", "65536"]
        assert account_line("subgid", NEW) == ["100000", "65536"]
        assert AFTER.stat().st_ino == baseline["home_inode"]
        assert (AFTER.stat().st_uid, AFTER.stat().st_gid) == (1000, 100)
        assert BEFORE.is_symlink() and BEFORE.resolve() == AFTER
        assert (AFTER / "sentinel").read_text() == "preserved\n"
        assert (AFTER / ".ssh/authorized_keys").read_text() == baseline["key"]
        assert (AFTER / ".ssh/authorized_keys").stat().st_uid == 1000
        assert (AFTER / ".ssh/authorized_keys").stat().st_mode & 0o777 == 0o600
        current = samba.snapshot("/etc/samba/smb.conf", NEW)
        for key in ("user_sid", "group_sid", "acct_ctrl", "credential_fingerprint"):
            assert current[key] == baseline["samba"][key], key
        assert run("loginctl", "show-user", NEW, "--property=Linger", "--value") == "yes"
        assert samba.verify_logins("/etc/samba/smb.conf", baseline["samba"], BACKUP) == {
            "buildfleet": True, "amahi": True,
        }

    def rollback():
        run("systemctl", "stop", "samba-smbd.service")
        quiesce(NEW)
        if BEFORE.is_symlink():
            assert BEFORE.resolve() == AFTER
            BEFORE.unlink()
        if AFTER.exists():
            assert not BEFORE.exists()
            AFTER.rename(BEFORE)
        for file in ACCOUNT_FILES:
            if (BACKUP / file).exists():
                shutil.copy2(BACKUP / file, Path("/etc", file))
        shutil.copy2(BACKUP / "passdb.tdb", "/var/lib/samba/private/passdb.tdb")
        Path("/run/account-user-map").write_text("")
        baseline = json.loads((BACKUP / "baseline.json").read_text())
        run("loginctl", "enable-linger", OLD)
        run("systemctl", "start", "user@1000.service")
        run(baseline["system"] + "/bin/switch-to-configuration", "test")
        run("systemctl", "start", "samba-smbd.service")
        assert run("id", "-u", OLD) == "1000"
        assert not AFTER.exists()
        assert account_line("shadow", OLD) == baseline["shadow"]
        assert BEFORE.stat().st_ino == baseline["home_inode"]
        current = samba.snapshot("/etc/samba/smb.conf", OLD)
        assert current == baseline["samba"]

    if sys.argv[1] == "prepare":
        prepare()
    elif sys.argv[1] == "forward":
        try:
            forward()
            verify()
        except BaseException:
            rollback()
            raise
    elif sys.argv[1] == "verify":
        verify()
    elif sys.argv[1] == "rollback":
        rollback()
    else:
        raise SystemExit("unknown rehearsal phase")
  '';
in
{
  name = "account-rename-rehearsal";
  nodes.machine = { ... }: {
    virtualisation.memorySize = 2048;
    virtualisation.cores = 2;
    users.users.${old} = {
      isNormalUser = true;
      uid = 1000;
      group = "users";
      home = "/home/${old}";
      initialPassword = "unix-test-password";
      extraGroups = [ "wheel" ];
      linger = true;
      autoSubUidGidRange = false;
      subUidRanges = [ { startUid = 100000; count = 65536; } ];
      subGidRanges = [ { startGid = 100000; count = 65536; } ];
    };
    security.sudo.wheelNeedsPassword = false;
    services.openssh.enable = true;
    services.samba = {
      enable = true;
      settings = {
        global = {
          security = "user";
          "passdb backend" = "tdbsam";
          "username map" = "/run/account-user-map";
        };
        Media = {
          path = "/srv/share";
          "read only" = false;
          "valid users" = [ old new ];
        };
      };
    };
    systemd.tmpfiles.rules = [
      "f /run/account-user-map 0600 root root -"
      "d /srv/share 0700 ${old} users -"
    ];
    environment.systemPackages = [ pkgs.python3 pkgs.samba pkgs.shadow ];
    environment.etc."account_rename_samba.py".source = ../scripts/account_rename_samba.py;
    environment.etc."account-rename-rehearsal.py".source = rehearsal;
    specialisation.renamed.configuration = {
      users.users.${old} = {
        name = lib.mkForce new;
        home = lib.mkForce "/home/${new}";
      };
      systemd.tmpfiles.rules = lib.mkForce [
        "f /run/account-user-map 0600 root root -"
        "d /srv/share 0700 ${new} users -"
      ];
    };
  };
  testScript = ''
    machine.start()
    machine.wait_for_unit("multi-user.target")
    machine.wait_for_unit("samba-smbd.service")
    machine.succeed("ssh-keygen -q -t ed25519 -N \"\" -f /root/client")
    machine.succeed("install -d -m 700 -o 1000 -g 100 /home/buildfleet/.ssh")
    machine.succeed("install -m 600 -o 1000 -g 100 /root/client.pub /home/buildfleet/.ssh/authorized_keys")
    machine.succeed("printf 'preserved\\n' > /home/buildfleet/sentinel; chown 1000:100 /home/buildfleet/sentinel")
    machine.succeed("printf 'share sentinel\\n' > /srv/share/sentinel; chown 1000:100 /srv/share/sentinel")
    machine.succeed("printf 'smb-test-password\\nsmb-test-password\\n' | smbpasswd -s -a buildfleet")
    machine.succeed("ssh -o StrictHostKeyChecking=no -i /root/client buildfleet@127.0.0.1 'test $(id -u) = 1000'")
    machine.succeed("python3 /etc/account-rename-rehearsal.py prepare")

    with subtest("complete rename and Nix activation preserve credentials and ownership"):
        machine.succeed("systemd-run --wait --pipe --collect --unit=rename-rehearsal python3 /etc/account-rename-rehearsal.py forward")
        machine.succeed("/run/current-system/specialisation/renamed/bin/switch-to-configuration test")
        machine.succeed("python3 /etc/account-rename-rehearsal.py verify")
        machine.succeed("ssh -o StrictHostKeyChecking=no -i /root/client amahi@127.0.0.1 'test $(id -u) = 1000 && test $HOME = /home/amahi && test $(sudo -n id -u) = 0'")
        machine.fail("getent passwd buildfleet")
        machine.succeed("smbclient //127.0.0.1/Media -U amahi%smb-test-password -c 'ls sentinel'")
        machine.succeed("smbclient //127.0.0.1/Media -U buildfleet%smb-test-password -c 'ls sentinel'")

    with subtest("rollback restores the original login, home, Samba account and generation"):
        machine.succeed("systemd-run --wait --pipe --collect --unit=rollback-rehearsal python3 /etc/account-rename-rehearsal.py rollback")
        machine.succeed("ssh -o StrictHostKeyChecking=no -i /root/client buildfleet@127.0.0.1 'test $HOME = /home/buildfleet'")
        machine.succeed("smbclient //127.0.0.1/Media -U buildfleet%smb-test-password -c 'ls sentinel'")
        machine.fail("getent passwd amahi")

    with subtest("failure after the Unix home move rolls back the partial migration"):
        machine.succeed("touch /root/inject-rename-failure")
        machine.fail("systemd-run --wait --pipe --collect --unit=failure-rehearsal python3 /etc/account-rename-rehearsal.py forward")
        machine.succeed("test -d /home/buildfleet && test ! -L /home/buildfleet && test ! -e /home/amahi")
        machine.succeed("ssh -o StrictHostKeyChecking=no -i /root/client buildfleet@127.0.0.1 'test $(id -u) = 1000'")
        machine.succeed("smbclient //127.0.0.1/Media -U buildfleet%smb-test-password -c 'ls sentinel'")
  '';
}
