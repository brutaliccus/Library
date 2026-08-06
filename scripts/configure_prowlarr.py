#!/usr/bin/env python3
"""Idempotent Prowlarr bootstrap for Library Site (ABB + Knaben).

Matches production on the Pi:
  - Sync API key into .env
  - Ensure native Knaben indexer is enabled
  - Ensure AudioBookBay Torznab points at Jackett
  - Reuse sync_prowlarr_abb_indexer wiring for ABB field updates

Skip path: set PROWLARR_URL + PROWLARR_API_KEY to an external instance.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.environ.get("LIBRARY_ENV_FILE", ROOT / ".env"))
CFG = Path(os.environ.get("PROWLARR_CONFIG", ROOT / "prowlarr-config" / "config.xml"))
JACKETT_CFG = Path(
    os.environ.get(
        "JACKETT_CONFIG",
        ROOT / "jackett-config" / "Jackett" / "ServerConfig.json",
    )
)
BUNDLED_URL = os.environ.get("PROWLARR_BUNDLED_URL", "http://audiobook-prowlarr:9696")
HOST_URL = os.environ.get("PROWLARR_HOST_URL", "http://127.0.0.1:9696").rstrip("/")
ABB_PATH = "/api/v2.0/indexers/audiobookbay/results/torznab/api"


def _jackett_base_url() -> str:
    """Prefer JACKETT_URL from .env (works for external Jackett)."""
    env = _load_env()
    for key in ("JACKETT_INTERNAL_URL", "JACKETT_URL"):
        val = (os.environ.get(key) or env.get(key) or "").strip().rstrip("/")
        if val and not _looks_placeholder(val):
            # Prowlarr-in-Docker cannot reach host loopback; rewrite common cases.
            if "127.0.0.1" in val or "localhost" in val:
                # Keep explicit override if operator set JACKETT_INTERNAL_URL.
                if key == "JACKETT_INTERNAL_URL":
                    return val
                continue
            return val
    return "http://audiobook-jackett:9117"
_DROP_FOR_PUT = frozenset({"indexerUrls", "added", "sortName", "capabilities"})


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
    return (
        "audiobook-prowlarr" in u
        or "://prowlarr" in u
        or (u.rstrip("/").endswith(":9696") and "127.0.0.1" in u)
    )


def _read_api_key_from_config() -> str:
    if not CFG.is_file():
        return ""
    try:
        root = ET.parse(CFG).getroot()
        el = root.find("ApiKey")
        return (el.text or "").strip() if el is not None else ""
    except Exception:
        return ""


def _read_jackett_key() -> str:
    env = _load_env()
    key = (env.get("JACKETT_API_KEY") or "").strip()
    if key and not _looks_placeholder(key):
        return key
    if JACKETT_CFG.is_file():
        try:
            return (json.loads(JACKETT_CFG.read_text(encoding="utf-8")).get("APIKey") or "").strip()
        except Exception:
            return ""
    return ""


def _wait_api(timeout: int = 180) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        key = _read_api_key_from_config()
        if key:
            try:
                _api("GET", "/ping", key, timeout=5)
                return key
            except Exception:
                try:
                    _api("GET", "/api/v1/system/status", key, timeout=5)
                    return key
                except Exception:
                    pass
        time.sleep(2)
    return _read_api_key_from_config()


def _api(
    method: str,
    path: str,
    key: str,
    body: dict | list | None = None,
    timeout: float = 45,
    base: str | None = None,
):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{(base or HOST_URL).rstrip('/')}{path}",
        data=data,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def _set_field(indexer: dict, name: str, value) -> bool:
    for field in indexer.get("fields") or []:
        if field.get("name") == name:
            if field.get("value") == value:
                return False
            field["value"] = value
            return True
    # Field missing from schema instance — append a minimal field.
    indexer.setdefault("fields", []).append({"name": name, "value": value})
    return True


def _payload_for_write(indexer: dict) -> dict:
    payload = dict(indexer)
    for key in _DROP_FOR_PUT:
        payload.pop(key, None)
    return payload


def _find_indexer(indexers: list, pred) -> dict | None:
    for item in indexers:
        if pred(item):
            return item
    return None


def _is_abb(item: dict) -> bool:
    name = (item.get("name") or "").lower().replace(" ", "")
    return "audiobook" in name and "bay" in name


def _is_knaben(item: dict) -> bool:
    name = (item.get("name") or "").lower()
    return "knaben" in name or (item.get("implementation") or "") == "Knaben"


def _schema_impl(key: str, implementation: str) -> dict | None:
    schema = _api("GET", "/api/v1/indexer/schema", key)
    if not isinstance(schema, list):
        return None
    # Prefer generic Torznab (empty / placeholder baseUrl) when requesting Torznab.
    if implementation == "Torznab":
        blank = None
        for item in schema:
            if item.get("implementation") != "Torznab":
                continue
            base = None
            for f in item.get("fields") or []:
                if f.get("name") == "baseUrl":
                    base = f.get("value")
                    break
            if base in (None, "", []):
                return item
            if blank is None:
                blank = item
        return blank
    for item in schema:
        if item.get("implementation") == implementation:
            return item
    return None


def ensure_knaben(key: str, indexers: list) -> list:
    existing = _find_indexer(indexers, _is_knaben)
    if existing:
        if not existing.get("enable"):
            existing["enable"] = True
            try:
                _api("PUT", f"/api/v1/indexer/{existing['id']}", key, _payload_for_write(existing))
                print(f"Enabled Knaben (id={existing['id']})")
            except Exception as e:
                print(f"warn: could not enable Knaben: {e}")
        else:
            print(f"Knaben already present (id={existing.get('id')})")
        return indexers

    template = _schema_impl(key, "Knaben")
    if not template:
        print("warn: Knaben not in Prowlarr schema — skip")
        return indexers
    template = dict(template)
    template["enable"] = True
    template["name"] = "Knaben"
    template["appProfileId"] = template.get("appProfileId") or 1
    template["priority"] = template.get("priority") or 25
    template["tags"] = template.get("tags") or []
    # Prefer knaben.org when select options exist.
    for field in template.get("fields") or []:
        if field.get("name") == "baseUrl" and field.get("type") == "select":
            field["value"] = field.get("value") or "https://knaben.org/"
    try:
        created = _api("POST", "/api/v1/indexer", key, _payload_for_write(template))
        print(f"Created Knaben indexer (id={created.get('id')})")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        print(f"warn: Knaben create failed HTTP {e.code}: {body[:400]}")
    except Exception as e:
        print(f"warn: Knaben create failed: {e}")
    return _api("GET", "/api/v1/indexer", key)


def ensure_abb_torznab(key: str, indexers: list, jackett_key: str) -> list:
    if not jackett_key:
        print("warn: Jackett API key missing — cannot wire AudioBookBay Torznab yet")
        return indexers

    jackett_base = _jackett_base_url()
    existing = _find_indexer(indexers, _is_abb)
    if existing:
        changed = False
        changed |= _set_field(existing, "baseUrl", jackett_base)
        changed |= _set_field(existing, "apiPath", ABB_PATH)
        changed |= _set_field(existing, "apiKey", jackett_key)
        if not existing.get("enable"):
            existing["enable"] = True
            changed = True
        if existing.get("name") != "AudioBookBay":
            existing["name"] = "AudioBookBay"
            changed = True
        if changed:
            for attempt in range(1, 13):
                try:
                    _api(
                        "PUT",
                        f"/api/v1/indexer/{existing['id']}",
                        key,
                        _payload_for_write(existing),
                    )
                    print(f"Updated AudioBookBay Torznab (id={existing['id']})")
                    break
                except urllib.error.HTTPError as e:
                    err = e.read().decode(errors="replace") if hasattr(e, "read") else str(e)
                    retryable = e.code in (400, 502, 503) and (
                        "Connection refused" in err
                        or "Unable to connect" in err
                        or "timed out" in err.lower()
                    )
                    if retryable and attempt < 12:
                        wait = min(5 * attempt, 30)
                        print(f"ABB update attempt {attempt}/12 — retry in {wait}s")
                        time.sleep(wait)
                        continue
                    print(f"warn: ABB update failed HTTP {e.code}: {err[:400]}")
                    break
                except Exception as e:
                    print(f"warn: ABB update failed: {e}")
                    break
        else:
            print(f"AudioBookBay already wired (id={existing.get('id')})")
        return indexers

    template = _schema_impl(key, "Torznab")
    if not template:
        print("warn: Torznab not in Prowlarr schema — skip ABB")
        return indexers
    template = dict(template)
    template["enable"] = True
    template["name"] = "AudioBookBay"
    template["appProfileId"] = template.get("appProfileId") or 1
    template["priority"] = 10
    template["tags"] = template.get("tags") or []
    _set_field(template, "baseUrl", jackett_base)
    _set_field(template, "apiPath", ABB_PATH)
    _set_field(template, "apiKey", jackett_key)
    print(f"Wiring AudioBookBay Torznab → {jackett_base}{ABB_PATH}")
    for attempt in range(1, 13):
        try:
            created = _api("POST", "/api/v1/indexer", key, _payload_for_write(template))
            print(f"Created AudioBookBay Torznab (id={created.get('id')})")
            return _api("GET", "/api/v1/indexer", key)
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace") if hasattr(e, "read") else str(e)
            retryable = e.code in (400, 502, 503) and (
                "Connection refused" in err
                or "Unable to connect" in err
                or "timed out" in err.lower()
            )
            if retryable and attempt < 12:
                wait = min(5 * attempt, 30)
                print(f"ABB create attempt {attempt}/12 — retry in {wait}s (Jackett warming)")
                time.sleep(wait)
                continue
            print(f"warn: ABB create failed HTTP {e.code}: {err[:400]}")
            break
        except Exception as e:
            print(f"warn: ABB create failed: {e}")
            break
    return indexers


def configure_bundled() -> int:
    print("Waiting for Prowlarr API ...")
    key = _wait_api()
    if not key:
        print("error: prowlarr configure failed (API key not ready)", file=sys.stderr)
        return 1
    _set_env("PROWLARR_URL", BUNDLED_URL)
    _set_env("PROWLARR_API_KEY", key)
    print("PROWLARR_URL / PROWLARR_API_KEY written to .env")

    try:
        indexers = _api("GET", "/api/v1/indexer", key)
    except Exception as e:
        print(f"warn: could not list indexers: {e}")
        return 0
    if not isinstance(indexers, list):
        indexers = []

    jackett_key = _read_jackett_key()
    indexers = ensure_knaben(key, indexers)
    ensure_abb_torznab(key, indexers if isinstance(indexers, list) else [], jackett_key)

    # Reuse production ABB sync helper when present (non-fatal).
    helper = ROOT / "scripts" / "sync_prowlarr_abb_indexer.py"
    if helper.is_file():
        try:
            import subprocess

            subprocess.run([sys.executable, str(helper), str(ROOT)], check=False)
        except Exception as e:
            print(f"warn: sync_prowlarr_abb_indexer: {e}")
    return 0


def configure_external(url: str, api_key: str) -> int:
    base = url.rstrip("/")
    key = api_key.strip()
    _set_env("PROWLARR_URL", base)
    _set_env("PROWLARR_API_KEY", key)
    print(f"Using existing Prowlarr at {base}")
    # Best-effort: still wire ABB/Knaben if the external instance is reachable.
    try:
        indexers = _api("GET", "/api/v1/indexer", key, base=base)
        if isinstance(indexers, list):
            jackett_key = _read_jackett_key()
            # Temporarily talk to the external host for ensure_* helpers.
            global HOST_URL
            prev = HOST_URL
            HOST_URL = base
            try:
                indexers = ensure_knaben(key, indexers)
                ensure_abb_torznab(key, indexers, jackett_key)
            finally:
                HOST_URL = prev
    except Exception as e:
        print(f"warn: could not preconfigure external Prowlarr indexers: {e}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--external-url", default=os.environ.get("PROWLARR_EXTERNAL_URL", ""))
    parser.add_argument(
        "--external-api-key",
        default=os.environ.get("PROWLARR_EXTERNAL_API_KEY", ""),
    )
    parser.add_argument("--force-bundled", action="store_true")
    args = parser.parse_args()

    env = _load_env()
    ext_url = (args.external_url or "").strip()
    ext_key = (args.external_api_key or "").strip()
    if ext_url and ext_key:
        return configure_external(ext_url, ext_key)

    existing_url = env.get("PROWLARR_URL", "")
    existing_key = env.get("PROWLARR_API_KEY", "")
    if (
        not args.force_bundled
        and not _looks_placeholder(existing_key)
        and existing_url
        and "prowlarr" not in existing_url.lower()
        and "127.0.0.1" not in existing_url
        and "localhost" not in existing_url
    ):
        print(f"skip prowlarr configure (external PROWLARR_URL already set: {existing_url})")
        return 0

    return configure_bundled()


if __name__ == "__main__":
    raise SystemExit(main())
