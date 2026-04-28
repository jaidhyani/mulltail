# mulltail

A self-hosted [Tailscale](https://tailscale.com) exit node that routes its
traffic through [Mullvad VPN](https://mullvad.net), with a web UI to switch
exit relays from anywhere on your tailnet.

```
your laptop          tailnet               mulltail container          internet
   ┌─┐    ── exit-node ──>   ┌──────────────────┐   wireguard   ┌────────┐
   │ │ ─────────────────────>│ tailscaled  +  wg │ ────────────> │ Mullvad │
   └─┘                       └──────────────────┘               └────────┘
                                       │
                                  http://host:8191
                              (relay-picker UI)
```

One Mullvad subscription. One Tailscale account. Any device on your tailnet
can use this as its exit node — including phones — and you can pick the
exit country/city from a map.

## Quickstart

You need:

- **Docker** (Desktop on macOS/Windows, or `docker-ce` on Linux). The exit-node
  container is privileged, see [Security](#security).
- A **Mullvad account** ([sign up](https://mullvad.net)). Free tier won't work;
  any paid plan does. The 5-device limit applies — `mulltail` consumes one slot.
- A **Tailscale auth key** — generate at
  [login.tailscale.com/admin/settings/keys](https://login.tailscale.com/admin/settings/keys).

Then:

```bash
git clone https://github.com/jaidhyani/mulltail
cd mulltail
./mulltail up
```

On first run it'll walk you through three prompts (account, auth key, region),
write the answers to `.env`, build the containers, and bring everything up.

After it boots:

1. Approve the new node at
   [login.tailscale.com/admin/machines](https://login.tailscale.com/admin/machines)
   and enable "Use as exit node."
2. Open `http://localhost:8191` to pick or switch the Mullvad exit relay.
3. On any tailnet device:

   ```bash
   tailscale up --exit-node=mulltail --exit-node-allow-lan-access
   ```

## Commands

```
./mulltail up            Build & start (interactive setup if no .env)
./mulltail down          Stop containers (state preserved)
./mulltail status        Show containers + exit IP + tailscale status
./mulltail logs [svc]    Tail logs (svc: exit-node | ui)
./mulltail config        Edit .env in $EDITOR
./mulltail destroy       Stop and wipe all state volumes
```

## Configuration

All config lives in **`.env`** (one file, human-readable). See
[`.env.example`](./.env.example) for the full list with comments.

Edit it with `./mulltail config` (or any editor) and re-apply with
`./mulltail up`.

## How it works

Two containers run side-by-side via `docker compose`:

- **`exit-node`** — Debian + `wireguard-tools` + `tailscale`. On first boot,
  registers a WireGuard pubkey with Mullvad's API, fetches a relay matching
  your `MULLTAIL_LOCATION`, writes `wg0.conf`, brings up the tunnel, then
  joins your tailnet with `--advertise-exit-node`. State persists in Docker
  volumes; subsequent boots reuse the same key + node identity.
- **`ui`** — FastAPI app listing all Mullvad relays on a Leaflet map.
  Clicking a marker rewrites the peer section of the running container's
  `wg0.conf` and `docker restart`s it (~5–10s blip per switch).

### Why raw `wg-quick` and not `mullvad-daemon`?

The obvious approach — install Mullvad's official Linux package and let its
daemon manage the tunnel — does not work for a Tailscale-exit-node sidecar.

`mullvad-daemon`'s tunnel-up state applies a strict firewall that allows only
traffic through the tunnel itself and traffic to Mullvad's API endpoints.
Tailscale needs to reach `controlplane.tailscale.com` and DERP relays on the
open internet — *not* through the Mullvad tunnel. Inside the same network
namespace, Mullvad's firewall blocks Tailscale's control connection, so
`tailscale up` hangs forever on bootstrap-DNS errors.

Mullvad's split-tunnel-by-PID feature would exempt `tailscaled`, but its
Linux implementation needs cgroups (`/sys/fs/cgroup/net_cls`) which Docker
doesn't expose r/w. So we cut the daemon out entirely: pull a relay's pubkey
+ endpoint from the public API, write a `wg0.conf`, run `wg-quick`. No
firewall, no daemon, no cgroup dependency. Tailscale runs alongside in the
same netns and works fine.

### Persisted state

Two Docker volumes survive `./mulltail down` and machine reboots:

- `mullvad-wg` → `/etc/mullvad-wg/wg0.conf`
- `ts-state` → `/var/lib/tailscale` (Tailscale machine + node keys)

`./mulltail destroy` wipes both. After that, the next `up` registers a fresh
Mullvad device and a fresh Tailscale node — your old entries linger as stale
records until you remove them manually.

## Security

- The `exit-node` container runs **privileged** with `/dev/net/tun`. WireGuard
  needs `NET_ADMIN`, `NET_RAW`, fwmark sysctls, and policy routing —
  enumerating the exact caps is fragile across Docker variants, so we follow
  the mainstream wg-in-docker pattern. If you're hosting alongside untrusted
  workloads, isolate the Docker host or run on a dedicated VM.
- The `ui` container mounts `/var/run/docker.sock`. That's effectively root
  on the host. **Do not expose port 8191 to the public internet.** Reach it
  via Tailscale (`tailscale serve`) or a reverse proxy with auth.
- `.env` contains your Mullvad account number and a Tailscale auth key. It's
  gitignored; `mulltail` writes it `chmod 600`. Treat it like a credential.

## Mullvad device slot

Mullvad caps accounts at 5 devices. Each fresh `wg`-pubkey registration
consumes a slot. `mulltail` registers exactly once, on the first boot, and
reuses the key forever. But:

- `./mulltail destroy` (or manually deleting the `mullvad-wg` volume) burns
  the existing slot. The next boot registers a new one.
- If you hit the 5-device cap, free a slot at
  [mullvad.net/account](https://mullvad.net/account) before retrying.

## License

MIT. See [LICENSE](./LICENSE).

Not affiliated with Mullvad or Tailscale.
