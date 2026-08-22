"""スナップショットの読み書き。

`data/snapshots/<YYYY-MM-DD>/<dataset>.jsonl` に**追記のみ**で貯める。
過去のファイルは書き換えない。これで「2026/xx/xx 時点」の断面がそのまま残り、
xlsx 側は全断面を読み直すだけで時系列を再構成できる。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .vehicles import DATA_DIR

SNAPSHOT_DIR = DATA_DIR / "snapshots"

DATASETS = (
    "auction_listings",  # ヤフオク落札明細（生）
    "price_by_year",  # 車種 × 年式 の相場（メイン系列）
    "vehicle_summary",  # 車種単位のサマリ（カーセンサー + 価格.com）
    "reviews",  # 口コミ明細（みんカラ）
    "review_summary",  # 車種 × 年式 の口コミ集計
    "recalls",  # 国交省リコール
    "defects",  # 国交省 不具合情報（明細）
    "defect_summary",  # 装置別の不具合集計 = 壊れやすい点
)


def today() -> str:
    return date.today().isoformat()


def snapshot_path(snapshot: str, dataset: str) -> Path:
    return SNAPSHOT_DIR / snapshot / f"{dataset}.jsonl"


def write(snapshot: str, dataset: str, rows: Iterable[dict[str, Any]]) -> int:
    path = snapshot_path(snapshot, dataset)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def read(snapshot: str, dataset: str) -> list[dict[str, Any]]:
    path = snapshot_path(snapshot, dataset)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def list_snapshots() -> list[str]:
    if not SNAPSHOT_DIR.exists():
        return []
    return sorted(p.name for p in SNAPSHOT_DIR.iterdir() if p.is_dir())


def read_all(dataset: str, snapshots: list[str] | None = None) -> list[dict[str, Any]]:
    """全スナップショット（または指定分）を時系列順に連結して返す。"""
    rows: list[dict[str, Any]] = []
    for snap in snapshots if snapshots is not None else list_snapshots():
        rows.extend(read(snap, dataset))
    return rows
