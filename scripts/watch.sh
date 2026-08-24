#!/usr/bin/env bash
# 出品中のオークションを見て、気になるものだけ Slack に流す。
# systemd timer から 1日4回呼ばれる想定（scripts/systemd/kakaku-ai-watch.timer）。
set -euo pipefail

cd "$(dirname "$0")/.."
PY="$(pwd)/.venv/bin/python"
[ -x "$PY" ] || { uv venv && uv pip install -e .; }

# 支払総額の上限。life#16 の条件に合わせてある。
BUDGET="${KAKAKU_BUDGET_MANYEN:-200}"

"$PY" -m kakaku_ai.cli -v watch --budget "$BUDGET"

# 既読リストが無限に伸びないよう、たまに整理する（出品は数日で消える）
git add data/watch_seen.json config/yahoo_brand_ids.json 2>/dev/null || true
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -q -m "data: watch 既読リストを更新 $(date +%F\ %H:%M)"
  git pull --rebase --autostash -q origin main && git push -q origin main
fi
