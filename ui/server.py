"""mulltail UI — pick a Mullvad exit relay for the mulltail-exit container.

Lists relays from Mullvad's public API, shows the current one, and on submit
rewrites the [Peer] of the container's wg0.conf and restarts the container.

Talks to the Docker daemon via the mounted socket — the UI runs in its own
container alongside the exit-node container.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


CONTAINER_NAME = os.environ.get("MULLTAIL_EXIT_CONTAINER", "mulltail-exit")
RELAY_API = "https://api.mullvad.net/public/relays/wireguard/v2/"
RELAY_PORT = 51820
RELAY_CACHE_TTL = 3600.0  # 1h
GEO_TTL = 30.0
DOCKER = "docker"

_switch_lock = threading.Lock()


@dataclass
class Relay:
    hostname: str
    country: str
    country_code: str
    city: str
    city_code: str
    ipv4: str
    pubkey: str
    lat: float
    lon: float
    weight: int
    active: bool


_relay_cache: dict[str, Any] = {"at": 0.0, "data": None}


def fetch_relays(force: bool = False) -> list[Relay]:
    now = time.time()
    if not force and _relay_cache["data"] is not None and now - _relay_cache["at"] < RELAY_CACHE_TTL:
        return _relay_cache["data"]

    with httpx.Client(timeout=10.0) as cx:
        r = cx.get(RELAY_API)
        r.raise_for_status()
        doc = r.json()

    locations = doc.get("locations", {})
    out: list[Relay] = []
    for w in doc["wireguard"]["relays"]:
        if not w.get("active"):
            continue
        loc_key = w.get("location") or ""
        m = re.match(r"^([a-z]{2})-([a-z0-9]+)$", loc_key)
        if not m:
            m = re.match(r"^([a-z]{2})-([a-z0-9]+)-wg-\d+$", w["hostname"])
            if not m:
                continue
        cc, city_code = m.group(1), m.group(2)
        loc = locations.get(loc_key, {})
        out.append(Relay(
            hostname=w["hostname"],
            country=loc.get("country", cc.upper()),
            country_code=cc,
            city=loc.get("city", city_code.upper()),
            city_code=city_code,
            ipv4=w["ipv4_addr_in"],
            pubkey=w["public_key"],
            lat=float(loc.get("latitude", 0.0)),
            lon=float(loc.get("longitude", 0.0)),
            weight=int(w.get("weight", 100)),
            active=True,
        ))
    out.sort(key=lambda r: (r.country, r.city, r.hostname))
    _relay_cache["at"] = now
    _relay_cache["data"] = out
    return out


_geo_cache: dict[str, Any] = {"at": 0.0, "data": None}


def current_geo(force_refresh: bool = False) -> dict[str, Any]:
    now = time.time()
    if (not force_refresh
            and _geo_cache["data"] is not None
            and now - _geo_cache["at"] < GEO_TTL):
        return _geo_cache["data"]
    try:
        out = subprocess.run(
            [DOCKER, "exec", CONTAINER_NAME, "curl", "-s",
             "--max-time", "3", "https://am.i.mullvad.net/json"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            data = json.loads(out.stdout)
            _geo_cache["at"] = now
            _geo_cache["data"] = data
            return data
    except Exception:
        pass
    return _geo_cache["data"] or {}


def read_wg_conf_endpoint() -> str | None:
    try:
        out = subprocess.run(
            [DOCKER, "exec", CONTAINER_NAME, "cat", "/etc/mullvad-wg/wg0.conf"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.lower().startswith("endpoint"):
                _, _, rhs = line.partition("=")
                return rhs.strip()
    except Exception:
        return None
    return None


def current_relay() -> Relay | None:
    ep = read_wg_conf_endpoint()
    if not ep:
        return None
    ip, _, _ = ep.partition(":")
    for r in fetch_relays():
        if r.ipv4 == ip:
            return r
    return None


def write_new_endpoint(relay: Relay) -> bool:
    """Rewrite the container's wg0.conf with new peer info, then restart it.

    Returns True when an actual switch happened, False when we're already
    on this relay (idempotent no-op).
    """
    cur_ep = read_wg_conf_endpoint()
    if cur_ep:
        cur_ip = cur_ep.split(":", 1)[0]
        if cur_ip == relay.ipv4:
            return False

    out = subprocess.run(
        [DOCKER, "exec", CONTAINER_NAME, "cat", "/etc/mullvad-wg/wg0.conf"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    conf = out.stdout

    new = re.sub(
        r"^PublicKey\s*=.*$",
        f"PublicKey = {relay.pubkey}",
        conf, flags=re.MULTILINE, count=1,
    )
    new = re.sub(
        r"^Endpoint\s*=.*$",
        f"Endpoint = {relay.ipv4}:{RELAY_PORT}",
        new, flags=re.MULTILINE, count=1,
    )
    if new == conf:
        return False

    p = subprocess.Popen(
        [DOCKER, "exec", "-i", CONTAINER_NAME, "sh", "-c",
         "cat > /etc/mullvad-wg/wg0.conf && chmod 600 /etc/mullvad-wg/wg0.conf"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    _, err = p.communicate(input=new.encode(), timeout=10)
    if p.returncode != 0:
        raise HTTPException(500, f"failed to write wg0.conf: {err.decode()[:200]}")

    res = subprocess.run(
        [DOCKER, "restart", CONTAINER_NAME],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        raise HTTPException(500, f"docker restart failed: {res.stderr[:200]}")

    _geo_cache["at"] = 0.0
    return True


class SetLocationBody(BaseModel):
    hostname: str = Field(..., min_length=3, max_length=64)


def relay_to_dict(r: Relay) -> dict:
    return {
        "hostname": r.hostname,
        "country": r.country,
        "country_code": r.country_code,
        "city": r.city,
        "city_code": r.city_code,
        "ipv4": r.ipv4,
        "lat": r.lat,
        "lon": r.lon,
        "weight": r.weight,
    }


def create_app() -> FastAPI:
    app = FastAPI(title="mulltail")

    @app.get("/api/health")
    def health():
        return {"ok": True, "container": CONTAINER_NAME}

    @app.get("/api/relays")
    def api_relays():
        return [relay_to_dict(r) for r in fetch_relays()]

    @app.get("/api/current")
    def api_current():
        cur = current_relay()
        return {
            "relay": relay_to_dict(cur) if cur else None,
            "geo": current_geo(),
        }

    @app.post("/api/set-location")
    def api_set(body: SetLocationBody):
        relays = fetch_relays()
        match = next((r for r in relays if r.hostname == body.hostname), None)
        if match is None:
            raise HTTPException(404, f"unknown relay: {body.hostname}")
        if not _switch_lock.acquire(timeout=0.0):
            raise HTTPException(409, "switch already in progress")
        try:
            changed = write_new_endpoint(match)
        finally:
            _switch_lock.release()
        return {"ok": True, "changed": changed, "relay": relay_to_dict(match)}

    @app.get("/api/wait-switch")
    def api_wait_switch(host: str, timeout_s: float = 18.0):
        start = time.time()
        last_err = None
        while time.time() - start < timeout_s:
            try:
                cur = current_relay()
                geo = current_geo(force_refresh=True)
                wg_ok = cur is not None and cur.hostname == host
                geo_host = geo.get("mullvad_exit_ip_hostname")
                geo_ok = geo_host == host
                if wg_ok and geo_ok:
                    return {
                        "ok": True,
                        "elapsed": round(time.time() - start, 2),
                        "relay": relay_to_dict(cur),
                        "geo": geo,
                    }
            except Exception as e:
                last_err = str(e)
            time.sleep(0.4)
        return {
            "ok": False,
            "elapsed": round(time.time() - start, 2),
            "relay": (relay_to_dict(current_relay()) if current_relay() else None),
            "geo": current_geo(),
            "error": last_err,
        }

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(PAGE_HTML)

    return app


app = create_app()


PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mulltail · pick an exit</title>
<base href="./">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Crimson+Pro:wght@400;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
  integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<style>
:root {
  --bg: #1e1e2e; --surface: #24243a; --surface2: #2a2a42;
  --border: rgba(205,214,244,0.10); --border-bright: rgba(205,214,244,0.22);
  --text: #cdd6f4; --text-dim: #a6adc8; --text-faint: #6c7086;
  --accent: #89dceb; --accent-soft: rgba(137,220,235,0.12);
  --good: #a6e3a1; --warn: #fab387;
  --shadow: 0 6px 24px rgba(0,0,0,0.30);
  --mono: 'JetBrains Mono','SF Mono',Consolas,monospace;
  --serif: 'Crimson Pro',Georgia,serif;
  --tile: dark;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #eff1f5; --surface: #fff; --surface2: #e6e9ef;
    --border: rgba(76,79,105,0.12); --border-bright: rgba(76,79,105,0.26);
    --text: #4c4f69; --text-dim: #6c6f85; --text-faint: #9ca0b0;
    --accent: #04a5e5; --accent-soft: rgba(4,165,229,0.10);
    --good: #40a02b; --warn: #fe640b;
    --shadow: 0 6px 24px rgba(76,79,105,0.14);
    --tile: light;
  }
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  background: var(--bg); color: var(--text); font-family: var(--mono);
  font-size: 14px; line-height: 1.55; min-height: 100vh;
  padding: 28px 22px 60px; max-width: 1100px; margin: 0 auto;
}
header { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin-bottom: 6px; }
h1 { font-family: var(--serif); font-size: 30px; letter-spacing: -0.5px; font-weight: 700; }
.tag { font-size: 10px; letter-spacing: 2px; text-transform: uppercase; color: var(--text-faint); }
.current {
  margin-top: 18px; margin-bottom: 18px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 20px;
  display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  position: relative; overflow: hidden;
}
.current::before {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background: var(--good); transition: background 0.2s;
}
.current.off::before { background: var(--warn); }
.current.switching::before { background: var(--accent); }
.status-pill {
  display: inline-flex; align-items: center; gap: 7px;
  font-size: 10px; letter-spacing: 1.8px; text-transform: uppercase;
  font-weight: 600; color: var(--good);
  border: 1px solid color-mix(in srgb, var(--good) 35%, transparent);
  background: color-mix(in srgb, var(--good) 10%, transparent);
  padding: 4px 10px; border-radius: 4px;
}
.status-pill .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.current.off .status-pill {
  color: var(--warn);
  border-color: color-mix(in srgb, var(--warn) 35%, transparent);
  background: color-mix(in srgb, var(--warn) 10%, transparent);
}
.current.switching .status-pill {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 35%, transparent);
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}
.current.switching .status-pill .dot { animation: pulse-dot 0.9s ease-in-out infinite; }
@keyframes pulse-dot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.7); }
}
.current-text { display: flex; flex-direction: column; gap: 3px; min-width: 0; flex: 1; }
.current-where { font-family: var(--serif); font-size: 22px; font-weight: 700; line-height: 1.15; }
.current-meta { color: var(--text-dim); font-size: 12px; }

.controls { display: flex; gap: 8px; align-items: stretch; margin-bottom: 14px; flex-wrap: wrap; }
.controls input {
  background: var(--surface); color: var(--text);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; font-family: inherit; font-size: 13px;
  flex: 1; min-width: 220px; transition: border-color 0.12s;
}
.controls input:focus { outline: none; border-color: var(--accent); }
.tabs { display: inline-flex; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 3px; gap: 2px; }
.tab {
  background: transparent; color: var(--text-dim); border: 0;
  padding: 7px 14px; font: inherit; font-size: 12px; cursor: pointer;
  border-radius: 6px; transition: background 0.12s, color 0.12s; letter-spacing: 0.5px;
}
.tab:hover { color: var(--text); }
.tab.active { background: var(--surface2); color: var(--text); }

.chips { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
.chip {
  background: var(--surface); color: var(--text-dim);
  border: 1px solid var(--border); border-radius: 999px;
  padding: 6px 12px; font: inherit; font-size: 12px; cursor: pointer;
  letter-spacing: 0.3px; transition: border-color 0.12s, color 0.12s, background 0.12s;
}
.chip:hover { color: var(--text); border-color: var(--border-bright); }
.chip.active { background: var(--accent-soft); color: var(--text); border-color: var(--accent); }
.chip-count { color: var(--text-faint); margin-left: 6px; font-size: 11px; }
.chip.active .chip-count { color: var(--text-dim); }

#map { height: 540px; border-radius: 10px; border: 1px solid var(--border); box-shadow: var(--shadow); background: var(--surface); }
.view-list { display: none; }
.view.active { display: block; }
#map-view.active { display: block; }

.continents { display: flex; flex-direction: column; gap: 8px; }
.continent { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; transition: border-color 0.12s; }
.continent:hover { border-color: var(--border-bright); }
.continent.open { border-color: var(--border-bright); }
.continent-head { display: flex; align-items: center; gap: 12px; padding: 14px 16px; cursor: pointer; user-select: none; }
.continent-name { font-family: var(--serif); font-size: 20px; font-weight: 700; flex: 1; }
.continent.has-current .continent-name::after { content: '●'; color: var(--accent); font-size: 14px; margin-left: 10px; }
.continent-body { display: none; border-top: 1px solid var(--border); padding: 8px; background: color-mix(in srgb, var(--surface2) 50%, transparent); }
.continent.open .continent-body { display: flex; flex-direction: column; gap: 5px; }

.country { background: var(--surface); border: 1px solid var(--border); border-radius: 7px; overflow: hidden; transition: border-color 0.12s; }
.country:hover { border-color: var(--border-bright); }
.country.open { border-color: var(--border-bright); }
.country-head { display: flex; align-items: center; gap: 10px; padding: 10px 12px; cursor: pointer; user-select: none; }
.chev { display: inline-block; width: 10px; transition: transform 0.15s; color: var(--text-faint); font-size: 11px; }
.country.open .chev, .continent.open .chev { transform: rotate(90deg); }
.country-name { font-family: var(--serif); font-size: 16px; font-weight: 700; flex: 1; }
.country-count, .continent-count { font-size: 11px; color: var(--text-faint); background: var(--surface2); padding: 2px 8px; border-radius: 999px; }
.country.has-current .country-name::after { content: '●'; color: var(--accent); font-size: 12px; margin-left: 8px; }
.country-body { display: none; border-top: 1px solid var(--border); padding: 6px; background: color-mix(in srgb, var(--surface2) 60%, transparent); }
.country.open .country-body { display: block; }

.cities { display: grid; grid-template-columns: repeat(auto-fill,minmax(240px,1fr)); grid-auto-rows: min-content; align-items: start; gap: 6px; }
.city { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; transition: border-color 0.12s, background 0.12s; }
.city:hover { border-color: var(--border-bright); }
.city.current { border-color: var(--accent); background: var(--accent-soft); }
.city.busy { opacity: 0.5; pointer-events: none; }
.city-main {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 10px 12px; cursor: pointer; width: 100%; min-height: 44px;
  background: transparent; color: inherit; border: 0; font: inherit;
  text-align: left; transition: background 0.12s;
}
.city-main:hover { background: var(--surface2); }
.city.current .city-main:hover { background: color-mix(in srgb, var(--accent) 18%, var(--surface)); }
.city-name { font-weight: 600; font-size: 13px; }
.city-count { font-size: 10px; color: var(--text-faint); letter-spacing: 0.5px; white-space: nowrap; }
.city.current .city-count::before { content: '● '; color: var(--accent); }
.city-spec { border-top: 1px solid var(--border); }
.city-spec summary { padding: 5px 12px; font-size: 10px; color: var(--text-faint); cursor: pointer; user-select: none; list-style: none; letter-spacing: 1px; text-transform: uppercase; }
.city-spec summary::-webkit-details-marker { display: none; }
.city-spec summary::before { content: '▸ '; transition: none; }
.city-spec[open] summary::before { content: '▾ '; }
.city-spec summary:hover { color: var(--text); }
.city-relays { display: grid; grid-template-columns: repeat(auto-fill,minmax(140px,1fr)); gap: 4px; padding: 4px 8px 8px; }
.city-relay {
  background: var(--surface2); border: 1px solid var(--border); border-radius: 4px;
  padding: 5px 8px; font-size: 11px; cursor: pointer; font-family: var(--mono);
  color: inherit; text-align: left;
  display: flex; align-items: center; justify-content: space-between; gap: 6px;
  transition: border-color 0.12s, background 0.12s;
}
.city-relay:hover { border-color: var(--accent); }
.city-relay.current { border-color: var(--accent); background: var(--accent-soft); }
.city-relay.busy { opacity: 0.5; pointer-events: none; }

.weight { font-size: 10px; font-family: var(--mono); color: var(--text-faint); padding: 1px 5px; border-radius: 3px; background: color-mix(in srgb, var(--text-faint) 12%, transparent); white-space: nowrap; }
.weight.high { color: var(--good); background: color-mix(in srgb, var(--good) 14%, transparent); }
.weight.low { color: var(--text-faint); opacity: 0.7; }
.weight.dropped { color: var(--text-faint); opacity: 0.5; text-decoration: line-through; }

.leaflet-popup-content-wrapper, .leaflet-popup-tip { background: var(--surface); color: var(--text); border: 1px solid var(--border-bright); box-shadow: var(--shadow); }
.leaflet-popup-content { margin: 12px 14px; font-family: var(--mono); font-size: 13px; line-height: 1.5; min-width: 200px; }
.leaflet-popup-content h3 { font-family: var(--serif); font-size: 17px; font-weight: 700; margin-bottom: 8px; color: var(--text); }
.leaflet-popup-content .country-sub { font-size: 11px; color: var(--text-faint); letter-spacing: 1px; text-transform: uppercase; margin-top: -4px; margin-bottom: 8px; }
.connect-btn {
  display: block; width: 100%; margin-top: 6px;
  padding: 10px 14px;
  background: var(--accent); color: var(--bg);
  border: 1px solid var(--accent); border-radius: 6px; cursor: pointer;
  font-family: var(--mono); font-weight: 600; font-size: 13px;
  letter-spacing: 0.5px; transition: opacity 0.12s, transform 0.06s;
}
.connect-btn:hover { opacity: 0.92; }
.connect-btn:active { transform: translateY(1px); }
.connect-btn.current { background: var(--accent-soft); color: var(--text); border-color: var(--accent); cursor: default; }
.connect-btn.busy { opacity: 0.5; pointer-events: none; }
.specific-section { margin-top: 10px; border-top: 1px solid var(--border); padding-top: 8px; }
.specific-section summary { font-size: 10px; color: var(--text-faint); cursor: pointer; letter-spacing: 1px; text-transform: uppercase; list-style: none; user-select: none; }
.specific-section summary::-webkit-details-marker { display: none; }
.specific-section summary::before { content: '▸ '; }
.specific-section[open] summary::before { content: '▾ '; }
.specific-section summary:hover { color: var(--text); }
.popup-relays { display: flex; flex-direction: column; gap: 3px; margin-top: 6px; }
.popup-relay {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  width: 100%;
  background: var(--surface2); color: var(--text);
  border: 1px solid var(--border); border-radius: 5px;
  padding: 6px 8px;
  font-family: var(--mono); font-size: 12px; cursor: pointer;
  text-align: left; transition: border-color 0.12s, background 0.12s;
}
.popup-relay:hover { border-color: var(--accent); }
.popup-relay.current { border-color: var(--accent); background: var(--accent-soft); }
.popup-relay.busy { opacity: 0.5; pointer-events: none; }
.leaflet-popup-close-button { color: var(--text-dim) !important; }
.leaflet-container { background: var(--surface) !important; }
.leaflet-control-attribution { background: rgba(0,0,0,0.4) !important; color: #aaa !important; }
.leaflet-control-attribution a { color: var(--accent) !important; }

.relay-marker { width: 12px; height: 12px; border-radius: 50%; background: var(--accent); border: 2px solid var(--bg); box-shadow: 0 1px 3px rgba(0,0,0,0.45); transition: transform 0.12s; }
.relay-marker:hover { transform: scale(1.35); }
.marker-current { position: relative; width: 14px; height: 14px; }
.marker-current::before { content: ''; position: absolute; inset: 0; border-radius: 50%; background: var(--good); border: 2px solid var(--bg); box-shadow: 0 1px 3px rgba(0,0,0,0.5); z-index: 2; }
.marker-current::after { content: ''; position: absolute; inset: -1px; border-radius: 50%; border: 2px solid var(--good); animation: marker-ping 1.8s cubic-bezier(0,0,0.2,1) infinite; z-index: 1; }
@keyframes marker-ping {
  0%   { transform: scale(0.8); opacity: 0.9; }
  80%  { transform: scale(2.6); opacity: 0; }
  100% { transform: scale(2.6); opacity: 0; }
}

.feedback {
  position: fixed; bottom: 18px; right: 18px; padding: 12px 16px;
  background: var(--surface); border: 1px solid var(--border-bright);
  border-radius: 8px; font-size: 13px; max-width: 340px;
  box-shadow: var(--shadow); z-index: 1000;
}
.feedback.error { color: var(--warn); }
.feedback.ok { color: var(--good); }
.empty { color: var(--text-faint); font-style: italic; padding: 24px 0; text-align: center; }
</style>
</head>
<body>
<header>
  <h1>mulltail</h1>
  <span class="tag">pick a Mullvad exit</span>
</header>

<div class="current off" id="current">
  <div class="current-text">
    <span class="status-pill" id="cur-pill"><span class="dot"></span><span id="cur-pill-label">offline</span></span>
    <div class="current-where" id="cur-where">…</div>
    <div class="current-meta" id="cur-meta"></div>
  </div>
</div>

<div class="controls">
  <input id="filter" placeholder="filter — country, city, or hostname (e.g. 'jp', 'tokyo', 'sea-001')">
  <div class="tabs" role="tablist">
    <button class="tab active" data-view="map">map</button>
    <button class="tab" data-view="list">list</button>
  </div>
</div>

<div class="chips" id="chips"></div>

<div id="map-view" class="view active"><div id="map"></div></div>
<div id="list-view" class="view">
  <div class="continents" id="continents"><div class="empty">loading relays…</div></div>
</div>

<div id="feedback" class="feedback" style="display:none;"></div>

<script>
let RELAYS = [];
let CURRENT = null;
let MAP = null;
let MARKERS = [];
let CITY_GROUPS = {};
let CONTINENT_FILTER = 'all';

const CONTINENT = {
  al:'EU', at:'EU', be:'EU', bg:'EU', ch:'EU', cy:'EU', cz:'EU', de:'EU',
  dk:'EU', ee:'EU', es:'EU', fi:'EU', fr:'EU', gb:'EU', gr:'EU', hr:'EU',
  hu:'EU', ie:'EU', it:'EU', nl:'EU', no:'EU', pl:'EU', pt:'EU', ro:'EU',
  rs:'EU', se:'EU', si:'EU', sk:'EU', tr:'EU', ua:'EU',
  hk:'AS', id:'AS', il:'AS', jp:'AS', my:'AS', ph:'AS', sg:'AS', th:'AS',
  ca:'NA', mx:'NA', us:'NA',
  ar:'SA', br:'SA', cl:'SA', co:'SA', pe:'SA',
  ng:'AF', za:'AF',
  au:'OC', nz:'OC',
};
const CONTINENT_NAMES = {
  EU: 'Europe', NA: 'North America', AS: 'Asia',
  SA: 'South America', OC: 'Oceania', AF: 'Africa',
  XX: 'Other',
};
const CONTINENT_BOUNDS = {
  EU: [[34, -15], [70, 42]],
  NA: [[12, -135], [62, -55]],
  AS: [[-12, 60], [55, 150]],
  SA: [[-56, -85], [15, -30]],
  OC: [[-50, 110], [0, 180]],
  AF: [[-37, -20], [38, 55]],
};
const CONTINENT_ORDER = ['EU', 'NA', 'AS', 'SA', 'OC', 'AF', 'XX'];

function continentOf(r) { return CONTINENT[r.country_code] || 'XX'; }

function maxWeight(relays) { return relays.reduce((m, r) => Math.max(m, r.weight ?? 100), 0); }
function pickRandom(relays) {
  if (relays.length === 1) return relays[0];
  const maxW = maxWeight(relays);
  const top = relays.filter(r => (r.weight ?? 100) === maxW);
  return top[Math.floor(Math.random() * top.length)];
}
function weightBadge(w, maxW) {
  let cls = '';
  if (w < maxW) cls = 'dropped';
  else if (w >= 200) cls = 'high';
  else if (w < 100) cls = 'low';
  return `<span class="weight ${cls}">w${w}</span>`;
}

function el(sel) { return document.querySelector(sel); }
function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function feedback(msg, kind) {
  const f = el('#feedback');
  f.textContent = msg;
  f.className = 'feedback ' + (kind || '');
  f.style.display = 'block';
  if (kind === 'ok') setTimeout(() => { f.style.display = 'none'; }, 4000);
}

function cityKey(r) { return `${r.country_code}-${r.city_code}`; }

function groupByCity(relays) {
  const groups = {};
  for (const r of relays) {
    const k = cityKey(r);
    if (!groups[k]) {
      groups[k] = { key: k, country: r.country, country_code: r.country_code, city: r.city, city_code: r.city_code, lat: r.lat, lon: r.lon, relays: [] };
    }
    groups[k].relays.push(r);
  }
  return groups;
}

function isDarkMode() { return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches; }

function initMap() {
  if (MAP) return;
  MAP = L.map('map', { worldCopyJump: true, minZoom: 2, maxZoom: 8, zoomControl: true, attributionControl: true }).setView([25, 10], 2);
  const dark = isDarkMode();
  const url = dark
    ? 'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png'
    : 'https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png';
  L.tileLayer(url, { attribution: '&copy; OpenStreetMap &copy; CARTO', subdomains: 'abcd' }).addTo(MAP);
}

function clearMarkers() {
  for (const m of MARKERS) MAP.removeLayer(m.marker);
  MARKERS = [];
}

function renderMap(filtered) {
  if (!MAP) initMap();
  clearMarkers();
  const groups = groupByCity(filtered);
  CITY_GROUPS = groups;
  for (const k of Object.keys(groups)) {
    const g = groups[k];
    if (!g.lat && !g.lon) continue;
    const isCurrent = CURRENT && cityKey(CURRENT) === k;
    const icon = L.divIcon({
      className: '',
      html: isCurrent ? `<div class="marker-current"></div>` : `<div class="relay-marker"></div>`,
      iconSize: isCurrent ? [14, 14] : [12, 12],
      iconAnchor: isCurrent ? [7, 7] : [6, 6],
    });
    const m = L.marker([g.lat, g.lon], { icon }).addTo(MAP);
    m.bindPopup(() => buildPopup(g), { maxWidth: 280, autoPan: true });
    MARKERS.push({ marker: m, cityKey: k, isCurrent });
  }
}

function buildPopup(g) {
  const div = document.createElement('div');
  const isCur = CURRENT && cityKey(CURRENT) === g.key;
  const n = g.relays.length;
  div.innerHTML = `
    <h3>${escapeHTML(g.city)}</h3>
    <div class="country-sub">${escapeHTML(g.country)} · ${n} relay${n === 1 ? '' : 's'}${isCur ? ' · current' : ''}</div>
    <button class="connect-btn${isCur ? ' current' : ''}">${isCur ? 'connected' : 'Connect'}</button>
  `;
  div.querySelector('.connect-btn').addEventListener('click', () => {
    if (isCur) return;
    pick(pickRandom(g.relays));
  });
  if (n > 1) {
    const det = document.createElement('details');
    det.className = 'specific-section';
    det.innerHTML = `<summary>specific server</summary><div class="popup-relays"></div>`;
    const list = det.querySelector('.popup-relays');
    const mw = maxWeight(g.relays);
    for (const r of g.relays) {
      const btn = document.createElement('button');
      btn.className = 'popup-relay' + (CURRENT && CURRENT.hostname === r.hostname ? ' current' : '');
      btn.innerHTML = `<span>${escapeHTML(r.hostname)}</span>${weightBadge(r.weight ?? 100, mw)}`;
      btn.addEventListener('click', () => pick(r));
      list.appendChild(btn);
    }
    div.appendChild(det);
  }
  return div;
}

function buildCityCard(c) {
  const isCurCity = CURRENT && cityKey(CURRENT) === c.key;
  const n = c.relays.length;
  const card = document.createElement('div');
  card.className = 'city' + (isCurCity ? ' current' : '');
  card.dataset.cityKey = c.key;
  card.innerHTML = `
    <button class="city-main">
      <span class="city-name">${escapeHTML(c.city)}</span>
      <span class="city-count">${n} ${n === 1 ? 'relay' : 'relays'}</span>
    </button>
  `;
  card.querySelector('.city-main').addEventListener('click', () => { pick(pickRandom(c.relays)); });
  if (n > 1) {
    const det = document.createElement('details');
    det.className = 'city-spec';
    det.innerHTML = `<summary>specific server</summary><div class="city-relays"></div>`;
    const list = det.querySelector('.city-relays');
    const mw = maxWeight(c.relays);
    for (const r of c.relays) {
      const btn = document.createElement('button');
      btn.className = 'city-relay' + (CURRENT && CURRENT.hostname === r.hostname ? ' current' : '');
      btn.innerHTML = `<span>${escapeHTML(r.hostname)}</span>${weightBadge(r.weight ?? 100, mw)}`;
      btn.addEventListener('click', (ev) => { ev.stopPropagation(); pick(r); });
      list.appendChild(btn);
    }
    card.appendChild(det);
  }
  return card;
}

function buildCountryCard(name, cityMap, isOpen, hasCurrent) {
  const cities = Object.values(cityMap);
  const totalRelays = cities.reduce((n, c) => n + c.relays.length, 0);
  const card = document.createElement('div');
  card.className = 'country' + (isOpen ? ' open' : '') + (hasCurrent ? ' has-current' : '');
  card.dataset.country = name;
  card.innerHTML = `
    <div class="country-head">
      <span class="chev">▶</span>
      <span class="country-name">${escapeHTML(name)}</span>
      <span class="country-count">${cities.length} ${cities.length === 1 ? 'city' : 'cities'} · ${totalRelays}</span>
    </div>
    <div class="country-body"></div>
  `;
  card.querySelector('.country-head').addEventListener('click', () => card.classList.toggle('open'));
  const body = card.querySelector('.country-body');
  const grid = document.createElement('div');
  grid.className = 'cities';
  for (const c of cities.sort((a, b) => a.city.localeCompare(b.city))) grid.appendChild(buildCityCard(c));
  body.appendChild(grid);
  return card;
}

function renderList(filtered) {
  const root = el('#continents');
  if (!filtered.length) { root.innerHTML = '<div class="empty">no relays match</div>'; return; }
  const q = el('#filter').value.trim();
  const curCountry = CURRENT ? CURRENT.country : null;
  const curContinent = CURRENT ? continentOf(CURRENT) : null;
  const wasContinentOpen = new Set([...root.querySelectorAll('.continent.open')].map(n => n.dataset.continent));
  const wasCountryOpen = new Set([...root.querySelectorAll('.country.open')].map(n => n.dataset.country));
  const bucket = {};
  for (const r of filtered) {
    const ck = continentOf(r);
    if (!bucket[ck]) bucket[ck] = {};
    if (!bucket[ck][r.country]) bucket[ck][r.country] = {};
    const cityK = `${r.country_code}-${r.city_code}`;
    if (!bucket[ck][r.country][cityK]) bucket[ck][r.country][cityK] = { city: r.city, key: cityK, relays: [] };
    bucket[ck][r.country][cityK].relays.push(r);
  }
  root.innerHTML = '';
  const noneWasOpen = wasContinentOpen.size === 0;
  const filterActive = q.length > 0 || CONTINENT_FILTER !== 'all';
  for (const ck of CONTINENT_ORDER) {
    if (!bucket[ck]) continue;
    const countries = bucket[ck];
    const countryNames = Object.keys(countries).sort();
    const totalCities = countryNames.reduce((n, c) => n + Object.keys(countries[c]).length, 0);
    const totalRelays = countryNames.reduce((n, c) => n + Object.values(countries[c]).reduce((m, city) => m + city.relays.length, 0), 0);
    const hasCurrent = curContinent === ck;
    const contOpen = filterActive || wasContinentOpen.has(ck) || (hasCurrent && noneWasOpen);
    const card = document.createElement('div');
    card.className = 'continent' + (contOpen ? ' open' : '') + (hasCurrent ? ' has-current' : '');
    card.dataset.continent = ck;
    card.innerHTML = `
      <div class="continent-head">
        <span class="chev">▶</span>
        <span class="continent-name">${escapeHTML(CONTINENT_NAMES[ck])}</span>
        <span class="continent-count">${countryNames.length} ${countryNames.length === 1 ? 'country' : 'countries'} · ${totalCities} ${totalCities === 1 ? 'city' : 'cities'} · ${totalRelays}</span>
      </div>
      <div class="continent-body"></div>
    `;
    card.querySelector('.continent-head').addEventListener('click', () => card.classList.toggle('open'));
    const body = card.querySelector('.continent-body');
    for (const name of countryNames) {
      const isCur = curCountry === name;
      const open = wasCountryOpen.has(name) || (q && true) || (isCur && noneWasOpen && hasCurrent);
      body.appendChild(buildCountryCard(name, countries[name], open, isCur));
    }
    root.appendChild(card);
  }
}

function renderChips() {
  const root = el('#chips');
  const counts = { all: RELAYS.length };
  for (const r of RELAYS) {
    const ck = continentOf(r);
    counts[ck] = (counts[ck] || 0) + 1;
  }
  const items = [{ key: 'all', label: 'All' }, ...CONTINENT_ORDER.filter(k => counts[k]).map(k => ({ key: k, label: CONTINENT_NAMES[k] }))];
  root.innerHTML = '';
  for (const it of items) {
    const b = document.createElement('button');
    b.className = 'chip' + (CONTINENT_FILTER === it.key ? ' active' : '');
    b.dataset.key = it.key;
    b.innerHTML = `${escapeHTML(it.label)}<span class="chip-count">${counts[it.key] || 0}</span>`;
    b.addEventListener('click', () => setContinent(it.key));
    root.appendChild(b);
  }
}

function setContinent(key) {
  CONTINENT_FILTER = key;
  document.querySelectorAll('.chip').forEach(c => c.classList.toggle('active', c.dataset.key === key));
  if (MAP) {
    if (key === 'all' || !CONTINENT_BOUNDS[key]) MAP.setView([25, 10], 2);
    else MAP.fitBounds(CONTINENT_BOUNDS[key], { padding: [20, 20] });
  }
  render();
}

function applyFilter() {
  const q = el('#filter').value.trim().toLowerCase();
  return RELAYS.filter(r => {
    if (CONTINENT_FILTER !== 'all' && continentOf(r) !== CONTINENT_FILTER) return false;
    if (!q) return true;
    return [r.hostname, r.country, r.country_code, r.city, r.city_code].join(' ').toLowerCase().includes(q);
  });
}

function render() {
  const filtered = applyFilter();
  renderMap(filtered);
  renderList(filtered);
}

function setStatus(state, label) {
  const card = el('#current');
  card.classList.remove('off', 'switching');
  if (state === 'off') card.classList.add('off');
  if (state === 'switching') card.classList.add('switching');
  el('#cur-pill-label').textContent = label || state;
}

async function loadCurrent() {
  try {
    const r = await fetch('api/current');
    const d = await r.json();
    CURRENT = d.relay;
    if (d.relay) {
      setStatus('active', 'active');
      el('#cur-where').textContent = `${d.relay.city}, ${d.relay.country}`;
      const geo = d.geo || {};
      const ip = geo.ip ? `IP ${geo.ip}` : '';
      const host = geo.mullvad_exit_ip_hostname ? `· ${geo.mullvad_exit_ip_hostname}` : `· ${d.relay.hostname}`;
      el('#cur-meta').textContent = [ip, host].filter(Boolean).join(' ');
    } else {
      setStatus('off', 'offline');
      el('#cur-where').textContent = '— not connected —';
      el('#cur-meta').textContent = '';
    }
    render();
  } catch (e) {
    setStatus('off', 'error');
    el('#cur-where').textContent = '— error —';
    el('#cur-meta').textContent = e.message;
  }
}

async function loadRelays() {
  const r = await fetch('api/relays');
  RELAYS = await r.json();
  renderChips();
  render();
}

let SWITCHING_TO = null;

async function pick(r) {
  if (CURRENT && CURRENT.hostname === r.hostname && SWITCHING_TO === null) {
    feedback(`already on ${r.hostname}`, 'ok');
    if (MAP) MAP.closePopup();
    return;
  }
  if (SWITCHING_TO) return;
  SWITCHING_TO = r.hostname;
  document.querySelectorAll('.relay, .popup-relay').forEach(d => d.classList.add('busy'));
  setStatus('switching', `→ ${r.city}`);
  el('#cur-where').textContent = `switching to ${r.city}, ${r.country}`;
  el('#cur-meta').textContent = r.hostname;
  feedback(`switching to ${r.hostname}…`, '');
  const t0 = performance.now();
  try {
    const resp = await fetch('api/set-location', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({hostname: r.hostname}),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const setData = await resp.json();
    if (setData.changed === false) {
      await loadCurrent();
      feedback(`already on ${r.hostname}`, 'ok');
      if (MAP) MAP.closePopup();
      return;
    }
    const wait = await fetch(`api/wait-switch?host=${encodeURIComponent(r.hostname)}`);
    const wd = await wait.json();
    await loadCurrent();
    const dt = ((performance.now() - t0) / 1000).toFixed(1);
    if (wd.ok) feedback(`now exiting from ${r.city}, ${r.country} · ${dt}s`, 'ok');
    else feedback(`switched, but verification timed out after ${dt}s — refresh to confirm`, 'error');
    if (MAP) MAP.closePopup();
  } catch (e) {
    feedback('error: ' + e.message, 'error');
    await loadCurrent();
  } finally {
    SWITCHING_TO = null;
    document.querySelectorAll('.relay, .popup-relay').forEach(d => d.classList.remove('busy'));
  }
}

function setView(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === name));
  el('#map-view').classList.toggle('active', name === 'map');
  el('#list-view').classList.toggle('active', name === 'list');
  el('#map-view').style.display = name === 'map' ? 'block' : 'none';
  el('#list-view').style.display = name === 'list' ? 'block' : 'none';
  if (name === 'map' && MAP) setTimeout(() => MAP.invalidateSize(), 50);
}

document.querySelectorAll('.tab').forEach(t => { t.addEventListener('click', () => setView(t.dataset.view)); });
el('#filter').addEventListener('input', () => render());

initMap();
loadRelays().then(loadCurrent);
setInterval(loadCurrent, 30000);
</script>
</body>
</html>
"""
