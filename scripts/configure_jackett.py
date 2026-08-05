#!/usr/bin/env python3
"""Idempotent Jackett bootstrap for Library Site (audiobook-oriented).

Mirrors production deploy steps on the Pi:
  - Point FlareSolverr at the compose sidecar with a long timeout
  - Ensure the AudioBookBay indexer is configured (audiobookbay.is)
  - Write JACKETT_URL / JACKETT_API_KEY into .env

Skip path: set JACKETT_URL + JACKETT_API_KEY to an external instance and exit.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.environ.get("LIBRARY_ENV_FILE", ROOT / ".env"))
CFG = Path(
    os.environ.get(
        "JACKETT_CONFIG",
        ROOT / "jackett-config" / "Jackett" / "ServerConfig.json",
    )
)
INDEXERS_DIR = CFG.parent / "Indexers"
ABB_CFG = INDEXERS_DIR / "audiobookbay.json"
FLARE_URL = os.environ.get("JACKETT_FLARESOLVERR_URL", "http://audiobook-flaresolverr:8191")
FLARE_TIMEOUT = int(os.environ.get("JACKETT_FLARE_TIMEOUT_MS", "180000"))
ABB_SITELINK = os.environ.get("JACKETT_ABB_SITELINK", "http://audiobookbay.is/")
BUNDLED_URL = os.environ.get("JACKETT_BUNDLED_URL", "http://audiobook-jackett:9117")
HOST_URL = os.environ.get("JACKETT_HOST_URL", "http://127.0.0.1:9117")
CONTAINER = os.environ.get("JACKETT_CONTAINER", "audiobook-jackett")


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_FILE.is_file():
        return out
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _set_env(key: str, value: str) -> None:
    if not ENV_FILE.is_file():
        ENV_FILE.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    lines = ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    found = False
    out: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def _looks_placeholder(value: str) -> bool:
    v = (value or "").strip().lower()
    return (not v) or v.startswith("your-") or v in {"changeme", "placeholder", "change-me"}


def _is_bundled_url(url: str) -> bool:
    u = (url or "").lower()
    return "audiobook-jackett" in u or "://jackett" in u


def _wait_config(timeout: int = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if CFG.is_file() and CFG.stat().st_size > 20:
            try:
                data = json.loads(CFG.read_text(encoding="utf-8"))
                if data.get("APIKey"):
                    return True
            except Exception:
                pass
        time.sleep(2)
    return CFG.is_file()


def _patch_server_config() -> str:
    data = json.loads(CFG.read_text(encoding="utf-8"))
    data["FlareSolverrUrl"] = FLARE_URL
    data["FlareSolverrMaxTimeout"] = FLARE_TIMEOUT
    data["AllowExternal"] = True
    CFG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    key = (data.get("APIKey") or "").strip()
    print(f"Jackett FlareSolverrUrl={FLARE_URL} timeout={FLARE_TIMEOUT}ms")
    return key


def _ensure_abb_indexer() -> bool:
    INDEXERS_DIR.mkdir(parents=True, exist_ok=True)
    desired = [
        {
            "id": "sitelink",
            "type": "inputstring",
            "name": "Site Link",
            "value": ABB_SITELINK,
        },
        {
            "id": "cookieheader",
            "type": "hiddendata",
            "name": "CookieHeader",
            "value": "",
        },
        {
            "id": "lasterror",
            "type": "hiddendata",
            "name": "LastError",
            "value": None,
        },
        {
            "id": "tags",
            "type": "inputtags",
            "name": "Tags",
            "value": "",
        },
    ]
    changed = False
    if ABB_CFG.is_file():
        try:
            current = json.loads(ABB_CFG.read_text(encoding="utf-8"))
        except Exception:
            current = []
        if isinstance(current, list):
            by_id = {item.get("id"): item for item in current if isinstance(item, dict)}
            sitelink = by_id.get("sitelink")
            if not sitelink or sitelink.get("value") != ABB_SITELINK:
                if sitelink:
                    sitelink["value"] = ABB_SITELINK
                else:
                    current.insert(0, desired[0])
                changed = True
                ABB_CFG.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
                print(f"Jackett ABB sitelink -> {ABB_SITELINK}")
            else:
                print("Jackett ABB indexer already configured")
            return changed
    ABB_CFG.write_text(json.dumps(desired, indent=2) + "\n", encoding="utf-8")
    print(f"Jackett ABB indexer created ({ABB_SITELINK})")
    return True


def _restart_jackett() -> None:
    try:
        subprocess.run(
            ["docker", "restart", CONTAINER],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except Exception as e:
        print(f"warn: could not restart {CONTAINER}: {e}")


def _probe_host(api_key: str, timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    urls = [
        f"{HOST_URL.rstrip('/')}/",
        f"{HOST_URL.rstrip('/')}/UI/Dashboard",
        f"{HOST_URL.rstrip('/')}/api/v2.0/indexers/audiobookbay/results/torznab/api?apikey={api_key}&t=caps",
    ]
    while time.time() < deadline:
        for url in urls:
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if 200 <= resp.status < 500:
                        print(f"Jackett host probe OK ({url.split('?')[0]})")
                        return True
            except urllib.error.HTTPError as e:
                if e.code in (200, 301, 302, 401, 403):
                    print(f"Jackett host reachable (HTTP {e.code})")
                    return True
            except Exception:
                pass
        time.sleep(2)
    print("warn: Jackett host probe timed out (continuing)")
    return False


def configure_bundled() -> int:
    print("Waiting for Jackett ServerConfig.json ...")
    if not _wait_config():
        print("skip jackett configure (config not ready)")
        return 0
    key = _patch_server_config()
    changed = _ensure_abb_indexer()
    if changed:
        _restart_jackett()
        time.sleep(3)
    if key:
        _set_env("JACKETT_URL", BUNDLED_URL)
        _set_env("JACKETT_API_KEY", key)
        print("JACKETT_URL / JACKETT_API_KEY written to .env")
        _probe_host(key)
    else:
        print("warn: Jackett API key empty after configure")
    return 0


def configure_external(url: str, api_key: str) -> int:
    _set_env("JACKETT_URL", url.rstrip("/"))
    _set_env("JACKETT_API_KEY", api_key.strip())
    print(f"Using existing Jackett at {url.rstrip('/')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--external-url",
        default=os.environ.get("JACKETT_EXTERNAL_URL", ""),
        help="Skip bundled configure; write this Jackett URL into .env",
    )
    parser.add_argument(
        "--external-api-key",
        default=os.environ.get("JACKETT_EXTERNAL_API_KEY", ""),
        help="API key for --external-url",
    )
    parser.add_argument(
        "--force-bundled",
        action="store_true",
        help="Configure bundled Jackett even if .env already has an external URL",
    )
    args = parser.parse_args()

    env = _load_env()
    ext_url = (args.external_url or "").strip()
    ext_key = (args.external_api_key or "").strip()
    if ext_url and ext_key:
        return configure_external(ext_url, ext_key)

    existing_url = env.get("JACKETT_URL", "")
    existing_key = env.get("JACKETT_API_KEY", "")
    if (
        not args.force_bundled
        and not _looks_placeholder(existing_key)
        and existing_url
        and not _is_bundled_url(existing_url)
    ):
        print(f"skip jackett configure (external JACKETT_URL already set: {existing_url})")
        return 0

    if not shutil.which("docker") and not CFG.is_file():
        print("skip jackett configure (no docker / no config)")
        return 0

    return configure_bundled()


if __name__ == "__main__":
    raise SystemExit(main())
