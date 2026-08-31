#!/usr/bin/env bash
# 出品中のオークションを見て、気になるものだけ Slack に流す。
# systemd timer から 1日4回呼ばれる想定（scripts/systemd/kakaku-ai-watch.timer）。
set -euo pipefail

cd "$(dirname "$0")/.."
PY="$(pwd)/.venv/bin/python"
[ -x "$PY" ] || { uv venv && uv pip install -e .; }

# 支払総額の上限。life#16 の条件に合わせてある。
BUDGET="${KAKAKU_BUDGET_MANYEN:-200}"

# 候補を 3 車種に絞った（life#16 のコメント参照）。国交省の不具合通報を
# 年式別に見て、世代交代と初期ロットが落ち着く年式から選んでいる。
#   フリード   2017年式〜  2代目。初代134件に対し7年で3件
#   ノア       2016年式〜  80系。2015年式は8.7件/100台でワースト
#   ヴォクシー 2017年式〜  80系。2017年式で1.5、2018年式で0.8まで落ちる
# 年式の下限は車種ごとに config/vehicles.*.yaml の model_year_from で持つ。
TARGETS="${KAKAKU_WATCH_TARGETS:-freed noah voxy}"

# 既定で修復歴「なし」の申告があるものだけに絞る
# shellcheck disable=SC2086
"$PY" -m kakaku_ai.cli -v watch --only $TARGETS --budget "$BUDGET" --repair none

# 既読リストが無限に伸びないよう、たまに整理する（出品は数日で消える）
git add data/watch_seen.json config/yahoo_brand_ids.json 2>/dev/null || true
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -q -m "data: watch 既読リストを更新 $(date +%F\ %H:%M)"
  git pull --rebase --autostash -q origin main && git push -q origin main
fi
