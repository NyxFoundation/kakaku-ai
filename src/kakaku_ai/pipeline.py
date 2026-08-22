"""週次クロールの本体。

1 回の実行 = 1 スナップショット。全ソースを順に叩き、正規化して
`data/snapshots/<日付>/` に落とす。過去分には一切触らない。
"""

from __future__ import annotations

import logging
from typing import Any

from . import aggregate, store
from .http import Fetcher
from .sources import carsensor, kakaku_com, minkara, mlit, yahoo_auction, yahoo_detail
from .vehicles import DATA_DIR, VehicleSet, load_vehicles

log = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR / "cache"


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
    sources = sources or {"yahoo", "carsensor", "kakaku", "minkara", "mlit"}

    targets = [v for v in vehicles if not only or v.key in only]
    log.info("snapshot=%s 車種=%s ソース=%s", snapshot, len(targets), sorted(sources))

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

        collected["vehicle_summary"].append(summary)
        collected["price_by_year"].extend(
            aggregate.merge_price_rows(
                auction_by_year,
                retail_by_year,
                vehicle,
                snapshot,
                model_year_from=vehicles.model_year_from,
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

    counts: dict[str, int] = {}
    for dataset in store.DATASETS:
        counts[dataset] = store.write(snapshot, dataset, collected[dataset])
    log.info("snapshot %s 書き出し完了: %s", snapshot, counts)
    return counts
