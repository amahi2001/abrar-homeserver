set -euo pipefail
name=nix-server.tailcb7cdb.ts.net
dns_name=$(tailscale status --json | jq -er '.Self.DNSName')
if [[ "${dns_name%.}" != "$name" ]]; then
  echo "Tailnet enrollment must assign $name before certificate issuance" >&2
  exit 1
fi
state=/var/lib/tailscale-nginx-cert
staged=$(mktemp -d "$state/.issuer-XXXXXXXX")
trap 'rm -rf "$staged"' EXIT
tailscale cert --min-validity=168h --cert-file "$staged/cert.pem" --key-file "$staged/key.pem" "$name"
openssl verify -purpose sslserver -verify_hostname "$name" -untrusted "$staged/cert.pem" "$staged/cert.pem"
openssl x509 -in "$staged/cert.pem" -pubkey -noout > "$staged/cert.pub"
openssl pkey -in "$staged/key.pem" -pubout > "$staged/key.pub"
cmp "$staged/cert.pub" "$staged/key.pub"
rm "$staged/"*.pub
# nginx loads the immutable generation name from the map at config reload.
map="$state/published-tailnet.map"
previous=""
if [[ -f "$map" ]]; then
  read -r previous_host directory < "$map" || true
  if [[ "$previous_host" == "$name" && "$directory" =~ ^(pair-[A-Za-z0-9]+)\;$ ]]; then previous="${BASH_REMATCH[1]}"; fi
fi
if [[ -n "$previous" ]] && cmp -s "$state/$previous/cert.pem" "$staged/cert.pem" && cmp -s "$state/$previous/key.pem" "$staged/key.pem" && [[ -r "$state/$previous/95-tailnet.cert" && -r "$state/$previous/95-tailnet.key" ]]; then
  version=$previous
else
  generation=$(mktemp -d "$state/pair-XXXXXXXX")
  chmod 0640 "$staged/"*.pem
  mv "$staged/"*.pem "$generation/"
  ln -s cert.pem "$generation/95-tailnet.cert"
  ln -s key.pem "$generation/95-tailnet.key"
  chmod 0750 "$generation"
  version=${generation##*/}
fi
printf '%s %s;\n' "$name" "$version" > "$staged/active-map"
chmod 0640 "$staged/active-map"
mv -Tf "$staged/active-map" "$map"

# Both consumers pin a complete version. A failed Cockpit start can be retried
# even if publication succeeded on the previous run.
if systemctl is-active --quiet nginx.service; then systemctl reload --no-block nginx.service; fi
ln -s "$version" "$staged/cockpit-current"
mv -Tf "$staged/cockpit-current" "$state/cockpit-current"
active=$(cat "$state/cockpit-active-generation" 2>/dev/null || true)
if systemctl is-failed --quiet cockpit.service; then
  systemctl start cockpit.service
elif [[ "$active" != "$version" ]]; then
  systemctl try-restart cockpit.service
fi
if [[ "$active" != "$version" ]]; then
  printf '%s\n' "$version" > "$staged/cockpit-active"
  mv -Tf "$staged/cockpit-active" "$state/cockpit-active-generation"
fi

# Retain valid generations for reloads and rollback. Remove only the expired
# random-name pairs generated here, after a further 30-day grace period.
retire_before=$(date -u -d '30 days ago' +%s)
for retired in "$state"/pair-????????; do
  [[ -d "$retired" && ! -L "$retired" && "${retired##*/}" != "$version" && "${retired##*/}" != "$previous" ]] || continue
  [[ -L "$retired/95-tailnet.cert" && -f "$retired/cert.pem" ]] || continue
  expiry=$(openssl x509 -in "$retired/cert.pem" -enddate -noout) || continue
  seconds=$(date -u -d "${expiry#notAfter=}" +%s) || continue
  if (( seconds < retire_before )); then
    rm -f -- "$retired/95-tailnet.cert" "$retired/95-tailnet.key" "$retired/cert.pem" "$retired/key.pem"
    rmdir -- "$retired"
  fi
done
