#!/usr/bin/env bash
# 週次: クロール → xlsx 生成 → Drive アップロード → main へ push
#
# systemd user timer から呼ばれる想定（scripts/systemd/ 参照）。手で叩いてもよい。
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
SNAPSHOT="$(date +%F)"

PY="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "==> .venv がないので作る"
  uv venv
  uv pip install -e .
fi

echo "==> crawl ($SNAPSHOT)"
"$PY" -m kakaku_ai.cli -v crawl --snapshot "$SNAPSHOT"

echo "==> excel"
"$PY" -m kakaku_ai.cli -v excel

echo "==> upload"
"$PY" -m kakaku_ai.cli -v upload --snapshot "$SNAPSHOT"

echo "==> push"
# auction_details.jsonl は落札商品ページの永続キャッシュ。これを持っていくことで
# 次回以降が新規分だけで済むので、一緒にコミットする。
git add data/snapshots data/xlsx data/auction_details.jsonl
if git diff --cached --quiet; then
  echo "変更なし"
else
  git commit -m "data: snapshot $SNAPSHOT"
  # 他所（GitHub Actions の手動実行など）が先に push していても落ちないように
  git pull --rebase --autostash origin main || {
    echo "rebase に失敗。手で解消してください" >&2
    exit 1
  }
  git push origin main
fi

# キャッシュは 8 週ぶんだけ残す（1 スナップショットで 50MB 前後）
find data/cache -maxdepth 1 -mindepth 1 -type d -mtime +56 -exec rm -rf {} + 2>/dev/null || true

echo "==> 完了 ($SNAPSHOT)"
