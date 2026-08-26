#!/usr/bin/env bash
# 全車種・全年式の小売相場を取り直して xlsx に反映する。
# 約2,200車種を1ページずつ舐めるので 75分前後かかる。週次本体とは別枠。
set -euo pipefail

cd "$(dirname "$0")/.."
PY="$(pwd)/.venv/bin/python"
[ -x "$PY" ] || { uv venv && uv pip install -e .; }

"$PY" -m kakaku_ai.cli -v wide
"$PY" -m kakaku_ai.cli -v excel
"$PY" -m kakaku_ai.cli -v upload

git add data/snapshots data/xlsx data/catalog.jsonl
if ! git diff --cached --quiet; then
  git commit -q -m "data: 全車種クロール $(date +%F)"
  git pull --rebase --autostash -q origin main && git push -q origin main
fi
