#!/usr/bin/env python3
"""
Discover latest Gemini model IDs via the Google AI models endpoint.
Caches results in .model-cache.json (TTL: 24h).

Usage:
  python3 discover.py                      # print resolved model map
  python3 discover.py --pattern "gemini.*flash"  # resolve one pattern
  python3 discover.py --refresh            # force cache refresh
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).parent
CACHE_FILE = SKILL_DIR / ".model-cache.json"
CACHE_TTL_SECONDS = 86400
PLAN_COUNCIL_ENV = Path.home() / ".plan-council" / ".env"
MASTER_ENV = Path.home() / ".claude" / "credentials" / "master.env"


def load_api_key():
    # Env vars take priority
    for key in ("GEMINI_API_KEY", "GEMINI_API_KEY_PAID"):
        val = os.environ.get(key)
        if val:
            return val
    # ~/.plan-council/.env (recommended), then ~/.claude/credentials/master.env (Claude Code users)
    for env_file in (PLAN_COUNCIL_ENV, MASTER_ENV):
        if not env_file.is_file():
            continue
        for prefix in ("GEMINI_API_KEY_PAID=", "GEMINI_API_KEY="):
            for line in env_file.read_text().splitlines():
                if line.startswith(prefix):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    return None


def load_cache():
    if not CACHE_FILE.is_file():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        if time.time() - data.get("timestamp", 0) < CACHE_TTL_SECONDS:
            return data.get("models", [])
    except Exception:
        pass
    return None


def fetch_models(api_key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    result = subprocess.run(
        ["curl", "-s", "--max-time", "15", url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        if "error" in data:
            print(f"[discover] API error: {data['error'].get('message', data['error'])}", file=sys.stderr)
            return None
        return [m["name"].removeprefix("models/") for m in data.get("models", [])]
    except Exception as e:
        print(f"[discover] Parse error: {e}", file=sys.stderr)
        return None


def save_cache(models):
    try:
        import tempfile
        tmp = CACHE_FILE.parent / (CACHE_FILE.name + ".tmp")
        tmp.write_text(json.dumps({"timestamp": time.time(), "models": models}, indent=2))
        tmp.replace(CACHE_FILE)
    except Exception:
        pass


def version_key(model_name):
    parts = re.findall(r"\d+", model_name)
    return tuple(int(x) for x in parts) if parts else (0,)


def resolve_pattern(pattern, models):
    rx = re.compile(pattern, re.IGNORECASE)
    matches = [m for m in models if rx.search(m)]
    if not matches:
        return None
    matches.sort(key=version_key, reverse=True)
    return matches[0]


def resolve_all(providers_json_path=None):
    if providers_json_path is None:
        providers_json_path = SKILL_DIR / "providers.json"
    config = json.loads(Path(providers_json_path).read_text())
    models = load_cache()
    if models is None:
        api_key = load_api_key()
        if not api_key:
            print("[discover] No Gemini API key found. Set GEMINI_API_KEY or add to ~/.plan-council/.env", file=sys.stderr)
            return {}
        models = fetch_models(api_key)
        if models is None:
            return {}
        save_cache(models)
    resolved = {}
    for name, cfg in config.get("providers", {}).items():
        if not cfg.get("enabled", False):
            continue
        if cfg.get("format") != "gemini":
            resolved[name] = cfg.get("model_id")
            continue
        model_id = cfg.get("model_id", "auto")
        if model_id != "auto":
            resolved[name] = model_id
        else:
            pattern = cfg.get("model_pattern")
            resolved[name] = resolve_pattern(pattern, models) if pattern else None
    return resolved


def main():
    parser = argparse.ArgumentParser(description="Discover latest Gemini model IDs")
    parser.add_argument("--pattern", help="Resolve a single regex pattern against the models list")
    parser.add_argument("--refresh", action="store_true", help="Force cache refresh")
    args = parser.parse_args()

    if args.refresh and CACHE_FILE.is_file():
        CACHE_FILE.unlink()

    if args.pattern:
        models = load_cache()
        if models is None:
            api_key = load_api_key()
            if not api_key:
                print("No Gemini API key. Set GEMINI_API_KEY or add to ~/.plan-council/.env", file=sys.stderr)
                sys.exit(1)
            models = fetch_models(api_key)
            if models is None:
                sys.exit(1)
            save_cache(models)
        result = resolve_pattern(args.pattern, models)
        print(result or "No match")
        return

    resolved = resolve_all()
    if not resolved:
        print("No models resolved (check API key or network)", file=sys.stderr)
        sys.exit(1)
    print("Resolved model IDs:")
    for provider, model in resolved.items():
        print(f"  {provider}: {model or 'NOT FOUND'}")


if __name__ == "__main__":
    main()
