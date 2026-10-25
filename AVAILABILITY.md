# Critical service availability

The critical services already start at boot through their NixOS modules. The
recovery timer covers services left stopped after startup or dependency failures
and services without native automatic restarts. Existing restart policies and
native start limits, including PostgreSQL's database protection, remain in
effect.

`critical-services-recovery.timer` runs two minutes after boot and one minute
after each check finishes. It uses the same 19-service list and application
health checks as guarded system updates: SSH, Tailscale, nginx, Docker, AdGuard,
Gluetun, Transmission, Vaultwarden, Jellyfin, Prowlarr, FlareSolverr, PostgreSQL,
Nextcloud PHP/Redis, Samba and Hermes gateway/dashboard.

The job starts inactive or failed critical services without restarting running
ones. It respects native start limits and leaves services that are starting or
stopping alone. The existing network and fail-closed VPN dependencies still
control startup. Sonarr, Radarr, development containers and CI runners retain
their existing on-demand or maintenance-controlled lifecycle.

Recovery takes the same nonblocking maintenance and host-deployment locks as
guarded updates. It skips checks while maintenance owns either lock, so it
cannot restart applications while their databases or files are being backed up
or restored. A manually stopped critical service will otherwise be started again
on the next check; remove it from the managed set before a permanent shutdown.

Health checks verify local DNS, the AdGuard UI, Nextcloud HTTPS readiness,
Vaultwarden, Jellyfin and Hermes. An unhealthy running service is reported
without killing active work. Failed checks appear in the journal and the
existing local maintenance failure records; no external notification service is
configured.

```bash
systemctl status critical-services-recovery.timer
journalctl -u critical-services-recovery.service -n 30
sudo system-update --check
sudo systemctl start critical-services-recovery.service
systemctl --failed
```

Hardware, storage, network, account authentication and external provider outages
still need their underlying problem resolved. These policies restore stopped
services; they do not guarantee uninterrupted uptime or automatically restart
unresponsive services that still have a running process.
