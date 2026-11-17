set -euo pipefail
name=nix-server.tailcb7cdb.ts.net
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
if [[ "$(cat "$state/cockpit-active-generation" 2>/dev/null || true)" != "$version" ]]; then
  if systemctl is-failed --quiet cockpit.service; then
    systemctl start cockpit.service
  else
    systemctl try-restart cockpit.service
  fi
  printf '%s\n' "$version" > "$staged/cockpit-active"
  mv -Tf "$staged/cockpit-active" "$state/cockpit-active-generation"
fi
