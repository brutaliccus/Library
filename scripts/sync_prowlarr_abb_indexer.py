#!/usr/bin/env python3
"""Fix Prowlarr AudioBook Bay Torznab → Jackett wiring and re-enable."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/opt/stacks/Library Site")
ENV_FILE = ROOT / ".env"
JACKETT_CFG = ROOT / "jackett-config/Jackett/ServerConfig.json"

PROWLARR_BASE = os.environ.get("PROWLARR_URL", "http://127.0.0.1:9696").rstrip("/")

# Prowlarr rejects some read-only fields on PUT.
_DROP_FOR_PUT = frozenset({"indexerUrls", "added", "sortName"})


def _load_env_key(name: str) -> str:
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return ""


def _api(method: str, path: str, key: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{PROWLARR_BASE}{path}",
        data=data,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def _set_field_value(indexer: dict, name: str, value) -> bool:
    for field in indexer.get("fields", []):
        if field.get("name") == name:
            if field.get("value") == value:
                return False
            field["value"] = value
            return True
    print(f"Warning: field {name!r} not found on indexer {indexer.get('name')!r}")
    return False


def _payload_for_put(indexer: dict) -> dict:
    payload = dict(indexer)
    for key in _DROP_FOR_PUT:
        payload.pop(key, None)
    return payload


def main() -> int:
    if not ENV_FILE.is_file() or not JACKETT_CFG.is_file():
        print(f"Missing config under {ROOT}")
        return 1

    prowlarr_key = _load_env_key("PROWLARR_API_KEY")
    jackett_key = json.loads(JACKETT_CFG.read_text()).get("APIKey", "")
    if not prowlarr_key or not jackett_key:
        print("PROWLARR_API_KEY or Jackett API key missing")
        return 1

    try:
        indexers = _api("GET", "/api/v1/indexer", prowlarr_key)
    except urllib.error.HTTPError as e:
        print(f"Failed to list Prowlarr indexers HTTP {e.code}")
        return 0
    except Exception as e:
        print(f"Failed to list Prowlarr indexers: {e}")
        return 0

    abb = next(
        (i for i in indexers if "audiobook" in (i.get("name") or "").lower() and "bay" in (i.get("name") or "").lower()),
        None,
    )
    if not abb:
        print("No AudioBook Bay indexer in Prowlarr")
        return 0

    want_base = "http://audiobook-jackett:9117"
    want_path = "/api/v2.0/indexers/audiobookbay/results/torznab/api"

    changed = False
    changed |= _set_field_value(abb, "baseUrl", want_base)
    changed |= _set_field_value(abb, "apiPath", want_path)
    changed |= _set_field_value(abb, "apiKey", jackett_key)
    if not abb.get("enable"):
        abb["enable"] = True
        changed = True

    if changed:
        payload = _payload_for_put(abb)
        last_err = ""
        for attempt in range(1, 13):
            try:
                _api("PUT", f"/api/v1/indexer/{abb['id']}", prowlarr_key, payload)
                print(f"Updated Prowlarr AudioBook Bay (id={abb['id']})")
                print(f"  baseUrl={want_base}")
                print(f"  apiPath={want_path}")
                break
            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode(errors="replace")
                except Exception:
                    pass
                last_err = err_body or str(e)
                retryable = e.code in (400, 502, 503) and (
                    "Connection refused" in last_err
                    or "Unable to connect" in last_err
                    or "timed out" in last_err.lower()
                )
                if retryable and attempt < 12:
                    wait = min(5 * attempt, 30)
                    print(
                        f"Prowlarr ABB update attempt {attempt}/12 failed "
                        f"(Jackett may still be starting) — retry in {wait}s"
                    )
                    import time

                    time.sleep(wait)
                    continue
                print(f"Prowlarr ABB update failed HTTP {e.code} — skipping (non-fatal for deploy)")
                if last_err:
                    print(last_err[:800])
                return 0
            except Exception as e:
                print(f"Prowlarr ABB update failed: {e} — skipping (non-fatal for deploy)")
                return 0
        else:
            print(f"Prowlarr ABB update gave up after retries — skipping (non-fatal for deploy)")
            if last_err:
                print(last_err[:800])
            return 0
    else:
        print("Prowlarr AudioBook Bay already correct and enabled")

    # Skip Indexer test by default — it wakes FlareSolverr/Chromium on every deploy
    # even when ABB is idle. Set ABB_INDEXER_TEST=1 to force a live test.
    if os.environ.get("ABB_INDEXER_TEST", "").strip() in ("1", "true", "yes"):
        try:
            _api("POST", "/api/v1/indexer/test", prowlarr_key, _payload_for_put(abb), timeout=120)
            print("Indexer test: OK")
        except urllib.error.HTTPError as e:
            print(f"Indexer test failed HTTP {e.code} — Jackett/FlareSolverr may be slow; check Jackett UI")
        except Exception as e:
            print(f"Indexer test failed: {e}")
    else:
        print("Indexer test: skipped (set ABB_INDEXER_TEST=1 to run; avoids idle FlareSolverr)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
