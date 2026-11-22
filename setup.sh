#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
if [ ! -f config.json ]; then cp config.example.json config.json; fi
if [ ! -f ipinfo_tokens.txt ]; then cp ipinfo_tokens.txt.example ipinfo_tokens.txt; fi
if [ ! -f speed_token.txt ]; then cp speed_token.txt.example speed_token.txt; fi
if [ ! -f ipapi_key.txt ]; then cp ipapi_key.txt.example ipapi_key.txt; fi
printf '%s\n' 'Setup complete. Edit config.json and ipinfo_tokens.txt, then run .venv/bin/python main.py'
