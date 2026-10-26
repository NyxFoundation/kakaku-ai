#!/usr/bin/env bash
# 中古車の在庫を見て、気になるものだけ Slack に流す。
# systemd timer から 1日4回呼ばれる想定（scripts/systemd/kakaku-ai-watch.timer）。
set -euo pipefail

cd "$(dirname "$0")/.."
PY="$(pwd)/.venv/bin/python"
[ -x "$PY" ] || { uv venv && uv pip install -e .; }

# 監視対象: MAZDA CX-8 の H30(2018)年式以降・4WD。
#
# **ヤフオク監視（kakaku-ai watch）から在庫監視（watch-stock）に替えてある。**
# CX-8 がヤフオクに出てこないため。実測で、落札は過去180日で 1台、出品中は 0台
# （車種カテゴリ 2084317224 は brand_id が解決すらできない）。
# 一方カーセンサーには 1,074台（うち 4WD 411台）載っている。
# ヤフオクを見ているかぎり、この車種は永久に通知が来ない。
#
# 年式の下限は H30（2018年式）。CX-8 は 2017年12月発売なので 2017年式は
# 立ち上がりの 61台しかなく、そこを外すと世代としては 1 世代きりになる。
# 上限 2024 は生産終了（2024年3月）に合わせたもので、それ以降の年式は存在しない。
CODE="${KAKAKU_WATCH_CODE:-MA_S094}"           # カーセンサーの車種コード（CX-8）
DRIVE="${KAKAKU_WATCH_DRIVE:-4WD}"             # 検索側で絞る。タイトル判定だと7%取りこぼす
YEAR_FROM="${KAKAKU_WATCH_YEAR_FROM:-2018}"    # H30
YEAR_TO="${KAKAKU_WATCH_YEAR_TO:-2024}"
BUDGET="${KAKAKU_BUDGET_MANYEN:-300}"          # 支払総額の上限（万円）

# 既定で修復歴「なし」の申告があるものだけに絞る
"$PY" -m kakaku_ai.cli -v watch-stock \
  --code "$CODE" --drive "$DRIVE" \
  --year-from "$YEAR_FROM" --year-to "$YEAR_TO" \
  --budget "$BUDGET" --repair none

# 既読リストが無限に伸びないよう、たまに整理する
# （掲載が消えた個体は watch_stock.prune_seen が落とす）
git add data/watch_seen.json config/yahoo_brand_ids.json 2>/dev/null || true
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -q -m "data: watch 既読リストを更新 $(date +%F\ %H:%M)"
  git pull --rebase --autostash -q origin main && git push -q origin main
fi
