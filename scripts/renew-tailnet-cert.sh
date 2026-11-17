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
digest=$(sha256sum < "$staged/cert.pem")
version="pair-${digest%% *}"
chmod 0750 "$staged"
chmod 0640 "$staged/"*.pem
if [[ ! -e "$state/$version" ]]; then mv -T "$staged" "$state/$version"; fi
# Both consumers see the same generation, including on interrupted renewal.
if [[ ! -d "$staged" ]]; then staged=$(mktemp -d "$state/.link-XXXXXXXX"); fi
ln -s "$version" "$staged/current"
mv -Tf "$staged/current" "$state/current"
if systemctl is-active --quiet nginx.service; then systemctl reload --no-block nginx.service; fi
