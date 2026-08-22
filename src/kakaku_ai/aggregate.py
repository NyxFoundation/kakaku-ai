"""生の落札レコードなどを「車種 × 年式 × スナップショット日」に畳む。

このモジュールが出す行が、時系列シートの 1 行になる。
"""

from __future__ import annotations

from typing import Any, Iterable

MANYEN = 10_000  # 1万円

# 修復歴。検索結果は NONE / EXISTS、商品ページは なし / あり / わからない を返すので、
# yahoo_detail 側で NONE / REPAIRED / UNKNOWN に寄せてある。
REPAIRED = frozenset({"EXISTS", "REPAIRED"})
# メーター交換・不明。距離が信用できないので価格の集計から外す。
BAD_MILEAGE = frozenset({"METER_REPLACEMENT", "UNKNOWN_MILEAGE"})


def is_usable(row: dict[str, Any]) -> bool:
    """価格集計に使ってよい落札か。

    外すのは「修復歴あり」と「メーター交換・距離不明」だけ。

    修復歴 **わからない** は外さない。個人出品では珍しくない申告で、
    これを落とすとサンプルが痩せる（2026-08-22 の実測で、2013年式以降が
    243 件 → 299 件。除外すると 23% 失う）。件数は `unknown_repair_n` に
    残してあるので、気になるときはそこで割り引いて読める。

    なお商品ページを取るまでは修復歴が空欄で、空欄は「あり」ではないので
    通っていた。詳細取得で「わからない」と判明した途端に落ちる、という
    取りこぼしを避ける意味もある。
    """
    return (
        row.get("mileage_type") not in BAD_MILEAGE
        and row.get("repair_type") not in REPAIRED
    )


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        raise ValueError("empty")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    frac = pos - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def _stats(values: list[float]) -> dict[str, float | int]:
    values = sorted(values)
    return {
        "n": len(values),
        "min": round(values[0], 1),
        "p25": round(_quantile(values, 0.25), 1),
        "median": round(_quantile(values, 0.5), 1),
        "mean": round(sum(values) / len(values), 1),
        "p75": round(_quantile(values, 0.75), 1),
        "max": round(values[-1], 1),
    }


def yahoo_by_year(
    rows: Iterable[dict[str, Any]], vehicle, snapshot: str, *, min_samples: int = 1
) -> list[dict[str, Any]]:
    """ヤフオクの落札明細を年式ごとに集計する。

    主系列は `is_usable()` を通ったものだけ。メーター交換車と修復歴あり車は
    価格が大きく下振れして年式別の相場を歪めるため、件数だけ `excluded_n` に残す。
    """
    buckets: dict[int, dict[str, list[Any]]] = {}
    for row in rows:
        year = row.get("model_year")
        price = row.get("price")
        if not year or not price:
            continue
        slot = buckets.setdefault(
            year,
            {"clean": [], "all": [], "mileage": [], "bids": [], "flagged": [], "unknown": 0},
        )
        slot["all"].append(price / MANYEN)
        slot["bids"].append(row.get("bid_count") or 0)
        if is_usable(row):
            slot["clean"].append(price / MANYEN)
            if row.get("mileage_km"):
                slot["mileage"].append(row["mileage_km"])
            if row.get("repair_type") == "UNKNOWN":
                slot["unknown"] += 1
        else:
            slot["flagged"].append(price / MANYEN)

    out: list[dict[str, Any]] = []
    for year in sorted(buckets):
        slot = buckets[year]
        base = slot["clean"] or slot["all"]
        if len(base) < min_samples:
            continue
        stats = _stats(base)
        mileage = sorted(slot["mileage"])
        out.append(
            {
                "snapshot_date": snapshot,
                "source": "yahoo_auction",
                "vehicle_key": vehicle.key,
                "vehicle_name": vehicle.name,
                "model_year": year,
                "generation": vehicle.generation_label(year * 100 + 6),
                "auction_n": stats["n"],
                "auction_min_manyen": stats["min"],
                "auction_p25_manyen": stats["p25"],
                "auction_median_manyen": stats["median"],
                "auction_mean_manyen": stats["mean"],
                "auction_p75_manyen": stats["p75"],
                "auction_max_manyen": stats["max"],
                "auction_median_mileage_km": mileage[len(mileage) // 2] if mileage else None,
                "auction_mean_bid_count": (
                    round(sum(slot["bids"]) / len(slot["bids"]), 1) if slot["bids"] else None
                ),
                "excluded_n": len(slot["flagged"]),
                "unknown_repair_n": slot["unknown"],
                "basis": (
                    "実走行・修復歴あり以外" if slot["clean"] else "全件（実走行の該当なし）"
                ),
            }
        )
    return out


def merge_price_rows(
    auction_rows: list[dict[str, Any]],
    retail_rows: list[dict[str, Any]],
    vehicle,
    snapshot: str,
    *,
    model_year_from: int,
) -> list[dict[str, Any]]:
    """オークション相場と小売相場を年式キーで突き合わせる。

    差額（小売 − オークション）と乖離率を出す。中古車屋の粗利や、
    「今このクルマは業販が強いのか小売が強いのか」の目安になる。
    """
    by_year: dict[int, dict[str, Any]] = {}

    for row in auction_rows:
        by_year.setdefault(row["model_year"], {})["auction"] = row
    for row in retail_rows:
        by_year.setdefault(row["model_year"], {})["retail"] = row

    out: list[dict[str, Any]] = []
    for year in sorted(by_year):
        if year < model_year_from:
            continue
        pair = by_year[year]
        auction = pair.get("auction") or {}
        retail = pair.get("retail") or {}

        auction_median = auction.get("auction_median_manyen")
        retail_median = retail.get("retail_median_manyen")
        spread = None
        spread_pct = None
        if auction_median and retail_median:
            spread = round(retail_median - auction_median, 1)
            spread_pct = round((retail_median / auction_median - 1) * 100, 1)

        out.append(
            {
                "snapshot_date": snapshot,
                "vehicle_key": vehicle.key,
                "vehicle_name": vehicle.name,
                "model_year": year,
                "generation": (auction.get("generation") or retail.get("generation") or ""),
                # --- オークション相場（ヤフオク!・終了180日） ---
                "auction_n": auction.get("auction_n"),
                "auction_min_manyen": auction.get("auction_min_manyen"),
                "auction_p25_manyen": auction.get("auction_p25_manyen"),
                "auction_median_manyen": auction_median,
                "auction_mean_manyen": auction.get("auction_mean_manyen"),
                "auction_p75_manyen": auction.get("auction_p75_manyen"),
                "auction_max_manyen": auction.get("auction_max_manyen"),
                "auction_median_mileage_km": auction.get("auction_median_mileage_km"),
                "auction_unknown_repair_n": auction.get("unknown_repair_n"),
                # --- 小売相場（カーセンサー掲載） ---
                "retail_n": retail.get("listing_count"),
                "retail_p25_manyen": retail.get("retail_p25_manyen"),
                "retail_median_manyen": retail_median,
                "retail_mean_manyen": retail.get("retail_mean_manyen"),
                "retail_p75_manyen": retail.get("retail_p75_manyen"),
                # --- 乖離 ---
                "retail_minus_auction_manyen": spread,
                "retail_premium_pct": spread_pct,
            }
        )
    return out
