#!/bin/bash
# mulltail exit-node — Mullvad WireGuard tunnel + Tailscale exit node.
#
# Architecture:
#   - WireGuard tunnel via raw `wg-quick`. We do NOT use mullvad-daemon —
#     its firewall blocks Tailscale's control-plane traffic, and its
#     split-tunnel feature needs cgroups Docker doesn't expose.
#   - On first run we register a wg pubkey with Mullvad's API, fetch a
#     relay matching MULLTAIL_LOCATION, and write /etc/mullvad-wg/wg0.conf.
#   - Subsequent runs reuse the persisted config. No API calls, no key
#     rotation, idempotent across restarts.
#   - Tailscale runs alongside in the same netns and reaches its control
#     plane fine since there's no Mullvad firewall.
set -e

: "${MULLVAD_ACCOUNT_NUMBER:?MULLVAD_ACCOUNT_NUMBER must be set}"
: "${TS_AUTHKEY:?TS_AUTHKEY must be set}"
: "${TS_HOSTNAME:=mulltail}"
: "${MULLTAIL_LOCATION:=us-sea}"   # e.g. "us-sea", "us-nyc", "de-fra"

CONFIG_DIR=/etc/mullvad-wg
WG_CONF="$CONFIG_DIR/wg0.conf"
mkdir -p "$CONFIG_DIR"

if [ ! -f "$WG_CONF" ]; then
    echo "[mulltail] no $WG_CONF; bootstrapping config"
    echo "[mulltail] generating wg keypair and registering with Mullvad"
    PRIV=$(wg genkey)
    PUB=$(echo "$PRIV" | wg pubkey)
    REG=$(curl -fsS -X POST https://api.mullvad.net/wg/ \
          --data-urlencode "account=$MULLVAD_ACCOUNT_NUMBER" \
          --data-urlencode "pubkey=$PUB") || {
        echo "[mulltail] FATAL: pubkey registration failed (max 5 devices/account?)" >&2
        exit 1
    }
    # Response: "10.x.x.x/32,fc00:...:y/128"
    IPV4=$(echo "$REG" | cut -d, -f1)
    IPV6=$(echo "$REG" | cut -d, -f2)
    if [[ -z "$IPV4" || "$IPV4" == "$REG" ]]; then
        echo "[mulltail] FATAL: unexpected API response: $REG" >&2; exit 1
    fi

    echo "[mulltail] fetching relay list (location: $MULLTAIL_LOCATION)"
    RELAYS=$(curl -fsS https://api.mullvad.net/public/relays/wireguard/v2/) || {
        echo "[mulltail] FATAL: could not fetch relay list" >&2; exit 1
    }
    PICK=$(echo "$RELAYS" | jq -r --arg loc "$MULLTAIL_LOCATION" '
        .wireguard.relays
        | map(select(.active == true and (.hostname | startswith($loc))))
        | sort_by(.hostname)
        | .[0]
    ')
    if [ "$PICK" = "null" ] || [ -z "$PICK" ]; then
        echo "[mulltail] FATAL: no relay matched $MULLTAIL_LOCATION" >&2; exit 1
    fi
    RELAY_HOST=$(echo "$PICK" | jq -r '.hostname')
    RELAY_IP=$(echo "$PICK" | jq -r '.ipv4_addr_in')
    RELAY_PUB=$(echo "$PICK" | jq -r '.public_key')
    RELAY_PORT=51820
    echo "[mulltail] selected relay: $RELAY_HOST ($RELAY_IP)"

    cat > "$WG_CONF" <<EOF
[Interface]
PrivateKey = $PRIV
Address = $IPV4, $IPV6
DNS = 10.64.0.1

[Peer]
PublicKey = $RELAY_PUB
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = $RELAY_IP:$RELAY_PORT
PersistentKeepalive = 25
EOF
    chmod 600 "$WG_CONF"
    echo "[mulltail] wrote $WG_CONF"
fi

echo "[mulltail] bringing up wg0"
wg-quick up "$WG_CONF" || {
    echo "[mulltail] FATAL: wg-quick up failed" >&2; exit 1
}

echo "[mulltail] verifying external IP via Mullvad"
EXT_IP=$(curl -s --max-time 15 https://am.i.mullvad.net/ip || echo FAILED)
echo "[mulltail] external IP: $EXT_IP"
if [ "$EXT_IP" = "FAILED" ]; then
    echo "[mulltail] FATAL: could not reach am.i.mullvad.net" >&2; exit 1
fi

echo "[mulltail] starting tailscaled"
tailscaled --state=/var/lib/tailscale/tailscaled.state \
           --tun=userspace-networking &
TS_PID=$!
sleep 3

echo "[mulltail] joining tailnet as exit node ($TS_HOSTNAME)"
tailscale up \
    --authkey="$TS_AUTHKEY" \
    --hostname="$TS_HOSTNAME" \
    --advertise-exit-node \
    --accept-routes

echo "[mulltail] tailscale status:"
tailscale status || true

echo "[mulltail] ready — traffic exits via Mullvad ($EXT_IP)"
echo "[mulltail] approve the exit node at https://login.tailscale.com/admin/machines"

wait -n "$TS_PID"
echo "[mulltail] tailscaled exited; tearing down"
wg-quick down "$WG_CONF" || true
exit 1
