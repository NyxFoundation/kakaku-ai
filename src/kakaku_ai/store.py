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
    "jmty_listings",  # ジモティー掲載明細（掲載価格。業者・個人混在）
    "carsensor_delisted",  # 掲載が消えた店頭在庫＝成約推定
    "wide_by_year",  # 全車種 × 年式 の小売相場
    "wide_summary",  # 全車種の車種単位サマリ（カタログ兼用）
    "classic_listings",  # 旧車（1988〜2001年式）の在庫明細（1台ずつ）
    "classic_auctions",  # 旧車のヤフオク落札明細（中古車ノード全体から抽出）
    "yahoo_used_cars",  # ヤフオク「中古車・新車」ノードの落札 全数（180日）
    "catalog_reviews",  # 全車種の口コミ個票（みんカラ）
    "catalog_review_summary",  # 全車種の口コミ集計（車種単位）
    "catalog_defects",  # 全車種の不具合個票（国交省）
    "catalog_defect_summary",  # 全車種の装置別 不具合集計
    "catalog_recalls",  # 全車種のリコール
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
    """JSONL を読む。**改行は `\n` だけで割る。**

    `str.splitlines()` は U+2028（行区切り）や U+0085 でも割ってしまう。
    ところが `json.dumps(ensure_ascii=False)` はこれらをエスケープしないので、
    口コミ本文に紛れ込むと 1 レコードが 2 行に割れて JSON として壊れる。
    実際に口コミ 1,404 車種ぶんで U+2028 が 3 個混ざっていて落ちた。
    """
    path = snapshot_path(snapshot, dataset)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n")
            if line.strip()]


def list_snapshots() -> list[str]:
    if not SNAPSHOT_DIR.exists():
        return []
    return sorted(p.name for p in SNAPSHOT_DIR.iterdir() if p.is_dir())


def latest_snapshot_with(dataset: str) -> str | None:
    """そのデータセットが**中身入りで**入っている最新のスナップショット日を返す。

    収集は必ずしも全データセットを一度に撮るわけではない。`wide` だけ回した日は
    その日のディレクトリに `wide_*` しか無いし、`--sources` を絞った日も同じ。
    「最新ディレクトリ」を一律に使うと、その日に撮らなかったものが全部 0 件で
    出てしまうので、データセットごとに新しいほうから探す。
    """
    for snap in reversed(list_snapshots()):
        path = snapshot_path(snap, dataset)
        if path.exists() and path.stat().st_size > 0:
            return snap
    return None


def read_latest(dataset: str) -> tuple[list[dict[str, Any]], str | None]:
    """最新の中身入りスナップショットからデータセットを読む。(行, 日付) を返す。"""
    snap = latest_snapshot_with(dataset)
    return (read(snap, dataset) if snap else []), snap


def read_all(dataset: str, snapshots: list[str] | None = None) -> list[dict[str, Any]]:
    """全スナップショット（または指定分）を時系列順に連結して返す。"""
    rows: list[dict[str, Any]] = []
    for snap in snapshots if snapshots is not None else list_snapshots():
        rows.extend(read(snap, dataset))
    return rows


def pooled_auction_listings() -> list[dict[str, Any]]:
    """全スナップショットの落札明細を `auction_id` で重複排除して 1 本にする。

    ヤフオクの落札検索は「終了180日間」しか返さない。週次で撮り続けると窓は
    重なるが、`auction_id` で名寄せすれば **実効期間は 180 日を超えて伸びていく**。
    半年回せば約 1 年ぶんの落札が貯まる計算で、年式ごとのサンプル数がそのぶん増える。

    同じ落札が複数スナップショットに出てきたときは、情報量の多い（= 年式や
    走行距離が埋まっている）ほうを残す。
    """
    pool: dict[str, dict[str, Any]] = {}
    for snap in list_snapshots():
        for row in read(snap, "auction_listings"):
            auction_id = row.get("auction_id")
            if not auction_id:
                continue
            row = dict(row)
            row.setdefault("first_seen_snapshot", row.get("snapshot_date"))
            existing = pool.get(auction_id)
            if existing is None:
                pool[auction_id] = row
                continue
            # 初出の日付は保つ
            row["first_seen_snapshot"] = existing.get("first_seen_snapshot")
            filled = sum(1 for k in ("model_year", "mileage_km", "grade") if row.get(k))
            was = sum(1 for k in ("model_year", "mileage_km", "grade") if existing.get(k))
            if filled >= was:
                pool[auction_id] = row
    return sorted(pool.values(), key=lambda r: r.get("end_time") or "")
