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
git add data/snapshots data/xlsx
if git diff --cached --quiet; then
  echo "変更なし"
else
  git commit -m "data: snapshot $SNAPSHOT"
  git push origin main
fi

# キャッシュは 8 週ぶんだけ残す（1 スナップショットで 50MB 前後）
find data/cache -maxdepth 1 -mindepth 1 -type d -mtime +56 -exec rm -rf {} + 2>/dev/null || true

echo "==> 完了 ($SNAPSHOT)"
