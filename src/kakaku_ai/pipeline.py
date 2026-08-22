"""週次クロールの本体。

1 回の実行 = 1 スナップショット。全ソースを順に叩き、正規化して
`data/snapshots/<日付>/` に落とす。過去分には一切触らない。
"""

from __future__ import annotations

import logging
from typing import Any

from . import aggregate, store
from .http import Fetcher
from .sources import (
    carsensor,
    carsensor_listings,
    jmty,
    kakaku_com,
    minkara,
    mlit,
    yahoo_auction,
    yahoo_detail,
)
from .vehicles import DATA_DIR, VehicleSet, load_vehicles

log = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR / "cache"

ALL_SOURCES = frozenset({"yahoo", "carsensor", "kakaku", "jmty", "minkara", "mlit", "stock"})


def run(
    snapshot: str | None = None,
    *,
    vehicles: VehicleSet | None = None,
    only: list[str] | None = None,
    sources: set[str] | None = None,
    use_cache: bool = True,
    detail: bool = True,
    detail_limit: int | None = None,
) -> dict[str, int]:
    snapshot = snapshot or store.today()
    vehicles = vehicles or load_vehicles()
    sources = sources or set(ALL_SOURCES)

    targets = [v for v in vehicles if not only or v.key in only]
    partial = bool(only) or sources != ALL_SOURCES
    log.info(
        "snapshot=%s 車種=%s ソース=%s%s",
        snapshot, len(targets), sorted(sources), "（部分実行: 既存を保持）" if partial else "",
    )

    fetcher = Fetcher(cache_dir=CACHE_DIR, snapshot=snapshot, use_cache=use_cache)
    # 落札商品ページは 1 件 250KB 前後ある。スナップショットごとに生 HTML を貯めると
    # すぐ GB 単位になるので、こちらはキャッシュせず、パース結果だけを
    # data/auction_details.jsonl に永続保存する（yahoo_detail 側の責務）。
    detail_fetcher = Fetcher(use_cache=False)

    collected: dict[str, list[dict[str, Any]]] = {name: [] for name in store.DATASETS}

    # リコールは車種ごとではなくメーカー単位で 1 回だけ引いて、あとでローカルに振り分ける
    all_recalls: list[dict[str, Any]] = []
    if "mlit" in sources:
        try:
            all_recalls = mlit.fetch_recalls(fetcher, vehicles.maker)
        except Exception as exc:  # noqa: BLE001
            log.error("mlit recall 取得に失敗: %s", exc)

    for vehicle in targets:
        log.info("● %s", vehicle.name)

        auction_rows: list[dict[str, Any]] = []
        auction_by_year: list[dict[str, Any]] = []
        if "yahoo" in sources and vehicle.yahoo_categories:
            try:
                auction_rows = yahoo_auction.collect(fetcher, vehicle, snapshot)
                if detail:
                    try:
                        yahoo_detail.enrich(detail_fetcher, auction_rows, limit=detail_limit)
                    except Exception as exc:  # noqa: BLE001
                        log.error("  yahoo detail %s: %s", vehicle.name, exc)
                else:
                    yahoo_detail.apply_to(auction_rows)
                # 商品ページで年式が埋まった行は世代が空のままなので引き直す
                for row in auction_rows:
                    if not row.get("generation") and row.get("model_year_month"):
                        row["generation"] = vehicle.generation_label(row["model_year_month"])
                dated = sum(1 for r in auction_rows if r.get("model_year"))
                log.info(
                    "  yahoo %s: 年式あり %s/%s件", vehicle.name, dated, len(auction_rows)
                )
                auction_by_year = aggregate.yahoo_by_year(auction_rows, vehicle, snapshot)
            except Exception as exc:  # noqa: BLE001
                log.error("  yahoo %s: %s", vehicle.name, exc)
        collected["auction_listings"].extend(auction_rows)

        retail_by_year: list[dict[str, Any]] = []
        summary: dict[str, Any] = {
            "snapshot_date": snapshot,
            "vehicle_key": vehicle.key,
            "vehicle_name": vehicle.name,
            "maker": vehicles.maker,
            "body_type": vehicles.body_type,
            "generations": " / ".join(g.label for g in vehicle.generations),
        }

        if "carsensor" in sources:
            try:
                result = carsensor.collect(fetcher, vehicle, snapshot)
                if result:
                    retail_by_year = result["by_year"]
                    summary.update(
                        {f"carsensor_{k}": v for k, v in result["summary"].items()
                         if k not in ("snapshot_date", "vehicle_key", "vehicle_name", "source")}
                    )
            except Exception as exc:  # noqa: BLE001
                log.error("  carsensor %s: %s", vehicle.name, exc)

        if "kakaku" in sources:
            try:
                result = kakaku_com.collect(fetcher, vehicle, snapshot)
                if result:
                    summary.update(
                        {f"kakaku_{k}": v for k, v in result.items()
                         if k not in ("snapshot_date", "vehicle_key", "vehicle_name", "source")}
                    )
            except Exception as exc:  # noqa: BLE001
                log.error("  kakaku %s: %s", vehicle.name, exc)

        private_by_year: list[dict[str, Any]] = []
        if "jmty" in sources:
            try:
                jmty_rows = jmty.collect(
                    fetcher, vehicle, snapshot, model_year_from=vehicles.model_year_from
                )
                collected["jmty_listings"].extend(jmty_rows)
                private_by_year = jmty.by_year(jmty_rows, vehicle, snapshot)
            except Exception as exc:  # noqa: BLE001
                log.error("  jmty %s: %s", vehicle.name, exc)

        collected["vehicle_summary"].append(summary)
        collected["price_by_year"].extend(
            aggregate.merge_price_rows(
                auction_by_year,
                retail_by_year,
                vehicle,
                snapshot,
                model_year_from=vehicles.model_year_from,
                private_rows=private_by_year,
            )
        )

        if "minkara" in sources:
            try:
                reviews = minkara.collect(fetcher, vehicle, snapshot)
                collected["reviews"].extend(reviews)
                collected["review_summary"].extend(minkara.summarize(reviews, vehicle, snapshot))
            except Exception as exc:  # noqa: BLE001
                log.error("  minkara %s: %s", vehicle.name, exc)

        if "mlit" in sources:
            recalls = mlit.normalize_recalls(all_recalls, vehicle, snapshot)
            collected["recalls"].extend(recalls)
            try:
                defects = mlit.fetch_defects(fetcher, vehicle, snapshot, vehicles.maker)
            except Exception as exc:  # noqa: BLE001
                log.error("  mlit defect %s: %s", vehicle.name, exc)
                defects = []
            collected["defects"].extend(defects)
            collected["defect_summary"].extend(
                mlit.summarize_defects(defects, recalls, vehicle, snapshot)
            )

    # 店頭在庫の個体追跡。消えた＝売れたとみなして成約価格を推定する。
    # 全車種ぶんまとめて 1 回。結果が出るのは 2 週目から。
    if "stock" in sources:
        try:
            carsensor_listings.track(
                fetcher,
                targets,
                snapshot,
                model_year_from=vehicles.model_year_from,
                model_year_to=int(snapshot[:4]),
            )
            collected["carsensor_delisted"] = carsensor_listings.delisted_rows(snapshot)
        except Exception as exc:  # noqa: BLE001
            log.error("carsensor在庫の追跡に失敗: %s", exc)

    counts: dict[str, int] = {}
    for dataset in store.DATASETS:
        rows = collected[dataset]
        if partial:
            rows = _merge_with_existing(snapshot, dataset, rows, only)
        counts[dataset] = store.write(snapshot, dataset, rows)
    log.info("snapshot %s 書き出し完了: %s", snapshot, counts)
    return counts


def _merge_with_existing(
    snapshot: str, dataset: str, rows: list[dict[str, Any]], only: list[str] | None
) -> list[dict[str, Any]]:
    """一部のソース／車種だけ回したときに、既存のスナップショットを消さない。

    `--sources jmty` のように絞って実行すると、回さなかったデータセットは
    空のまま書き出されて、その日のスナップショットが壊れる。実際に一度やった。
    そこで部分実行のときは、
      * そのデータセットを 1 行も作っていなければ既存をそのまま残す
      * `--only` で車種を絞ったなら、対象外の車種の行は既存から引き継ぐ
    """
    existing = store.read(snapshot, dataset)
    if not existing:
        return rows
    if not rows:
        return existing
    if only:
        targeted = set(only)
        kept = [r for r in existing if r.get("vehicle_key") not in targeted]
        return kept + rows
    return rows
