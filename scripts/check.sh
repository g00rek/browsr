#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

python3 -m unittest discover -s tests -v
python3 -m py_compile bridge.py native_host.py browsr-chrome-mcp.py
node --check extension/service-worker.js
python3 -c 'import json, pathlib, tomllib; json.loads(pathlib.Path("extension/manifest.json").read_text()); tomllib.loads(pathlib.Path("herdr-plugin.toml").read_text())'
