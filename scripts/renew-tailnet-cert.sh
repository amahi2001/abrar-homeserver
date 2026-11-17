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
if [[ -n "$previous" ]] && cmp -s "$state/$previous/cert.pem" "$staged/cert.pem" && cmp -s "$state/$previous/key.pem" "$staged/key.pem"; then
  version=$previous
else
  generation=$(mktemp -d "$state/pair-XXXXXXXX")
  chmod 0640 "$staged/"*.pem
  mv "$staged/"*.pem "$generation/"
  chmod 0750 "$generation"
  version=${generation##*/}
fi
printf '%s %s;\n' "$name" "$version" > "$staged/active-map"
chmod 0640 "$staged/active-map"
mv -Tf "$staged/active-map" "$map"

# Cockpit selects the last .cert by filename. Install the matching immutable
# key link first. An interrupted publication leaves the previous choice valid.
cockpit=/etc/cockpit/ws-certs.d
mkdir -p "$cockpit"
shopt -s nullglob
certs=("$cockpit"/95-buildfleet-*.cert)
last=""
if (( ${#certs[@]} )); then last=${certs[-1]}; fi
if [[ -z "$last" || "$(readlink "$last" || true)" != "$state/$version/cert.pem" || "$(readlink "${last%.cert}.key" || true)" != "$state/$version/key.pem" ]]; then
  stem="$cockpit/95-buildfleet-$(date -u +%s%N)"
  ln -s "$state/$version/key.pem" "$stem.key"
  ln -s "$state/$version/cert.pem" "$stem.cert"
  systemctl try-restart cockpit.service
fi
if systemctl is-active --quiet nginx.service; then systemctl reload --no-block nginx.service; fi
