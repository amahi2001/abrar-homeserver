# The published proxy is reachable from this host and its project bridges only.
# Authentication remains mandatory in Headroom; no LAN/Tailscale listener is added.
exec 9>/run/lock/token-eye-network-guard.lock
flock 9
iptables -w -S DOCKER-USER >/dev/null
iptables -w -N TOKEN-EYE-PROXY 2>/dev/null || true
iptables -w -F TOKEN-EYE-PROXY
iptables -w -A TOKEN-EYE-PROXY -i docker0 -j RETURN
iptables -w -A TOKEN-EYE-PROXY -i br+ -j RETURN
iptables -w -A TOKEN-EYE-PROXY -j DROP
if ! iptables -w -C DOCKER-USER -p tcp -m conntrack --ctorigdst 172.17.0.1 --ctorigdstport 8787 -j TOKEN-EYE-PROXY 2>/dev/null; then
  iptables -w -I DOCKER-USER 1 -p tcp -m conntrack --ctorigdst 172.17.0.1 --ctorigdstport 8787 -j TOKEN-EYE-PROXY
fi
