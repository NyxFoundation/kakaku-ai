"""ヤフオク! の落札相場（終了180日間）を車種カテゴリ単位で集める。

`/closedsearch/closedsearch` は robots.txt で明示的に Allow されている。
一方で禁止クエリパラメータがいくつもあるので、`FORBIDDEN_PARAMS` で
うっかり付けないよう縛ってある（`n` = ページ件数 も禁止なので既定 50 のまま）。

1件ずつの `carSpec.modelDate` が年式なので、年式別の落札相場をそのまま作れる。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from ..http import Fetcher

log = logging.getLogger(__name__)

BASE = "https://auctions.yahoo.co.jp/closedsearch/closedsearch"
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
PAGE_SIZE = 50
MAX_PAGES = 20  # 50 x 20 = 1000件。1車種の180日落札はこれで足りる

# 車両スペック (carSpec = 年式・走行距離・修復歴) は「中古車・新車」ノードで検索した
# ときだけレスポンスに載る。車種リーフカテゴリを直接指定すると付いてこない（実測）。
# そこでこのノード + 車種名キーワードで引き、リーフカテゴリ ID で絞り込む。
USED_CAR_CATEGORY = 26360

# robots.txt で /closedsearch/*?*<param>= が Disallow されているもの
FORBIDDEN_PARAMS = frozenset(
    {
        "istatus",
        "abatch",
        "aucminprice",
        "aucmaxprice",
        "jpypayment",
        "pstagefree",
        "offer",
        "thumb",
        "select",
        "n",
        "wheel_spec_id",
    }
)


def _check_params(params: dict[str, Any]) -> None:
    bad = FORBIDDEN_PARAMS & set(params)
    if bad:
        raise ValueError(f"robots.txt で禁止されたパラメータを付けようとしています: {sorted(bad)}")


def _parse_next_data(html: str) -> dict[str, Any]:
    m = NEXT_DATA.search(html)
    if not m:
        raise ValueError("__NEXT_DATA__ が見つかりません（ページ構造が変わった可能性）")
    return json.loads(m.group(1))


def _model_year_month(model_date: Any) -> int | None:
    """carSpec.modelDate (20240401) -> 202404"""
    if not model_date:
        return None
    s = str(model_date)
    if len(s) < 6:
        return None
    try:
        ym = int(s[:6])
    except ValueError:
        return None
    if not (190001 <= ym <= 210012):
        return None
    return ym


def _search(fetcher: Fetcher, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """落札検索を全ページ舐める。

    `MAX_PAGES` で頭打ちになったら警告を出す。黙って切り捨てると
    「全部取れている」と誤解したまま相場を出すことになる。
    """
    total: int | None = None
    for page in range(MAX_PAGES):
        query = dict(params)
        query["b"] = page * PAGE_SIZE + 1
        _check_params(query)
        data = _parse_next_data(fetcher.get_text(BASE, query))
        listing = data["props"]["pageProps"]["initialState"]["search"]["items"]["listing"]

        if total is None:
            total = listing.get("totalResultsAvailable", 0)

        items = listing.get("items") or []
        if not items:
            return
        yield from items
        if (page + 1) * PAGE_SIZE >= (total or 0):
            return

    if total and total > MAX_PAGES * PAGE_SIZE:
        log.warning(
            "  yahoo: %s 件のうち先頭 %s 件で打ち切った（params=%s）。"
            "MAX_PAGES を上げないと取りこぼす。",
            total,
            MAX_PAGES * PAGE_SIZE,
            {k: v for k, v in params.items() if k != "b"},
        )


def _normalize(item: dict[str, Any], vehicle, snapshot: str) -> dict[str, Any] | None:
    price = item.get("price")
    auction_id = item.get("auctionId")
    if not price or not auction_id:
        return None

    spec = item.get("carSpec") or {}
    ym = _model_year_month(spec.get("modelDate"))
    return {
        "snapshot_date": snapshot,
        "source": "yahoo_auction",
        "vehicle_key": vehicle.key,
        "vehicle_name": vehicle.name,
        "auction_id": auction_id,
        "title": item.get("title", ""),
        "category_id": (item.get("category") or {}).get("id"),
        "category_name": (item.get("category") or {}).get("name"),
        "price": int(price),
        "buy_now_price": item.get("buyNowPrice"),
        "bid_count": item.get("bidCount"),
        "end_time": item.get("endTime"),
        "model_year_month": ym,
        "model_year": ym // 100 if ym else None,
        "generation": vehicle.generation_label(ym),
        "mileage_km": spec.get("mileage"),
        "mileage_type": spec.get("mileageType"),
        "repair_type": spec.get("repairType"),
        "overhead_costs": spec.get("overheadCosts"),
        "prefecture_code": item.get("prefectureCode"),
        "is_fixed_price": item.get("isFixedPrice"),
        "url": f"https://page.auctions.yahoo.co.jp/jp/auction/{auction_id}",
    }


def collect(fetcher: Fetcher, vehicle, snapshot: str) -> list[dict[str, Any]]:
    """1車種ぶんの落札レコードを正規化して返す。

    2 パスで取る:

    1. 「中古車・新車」ノード + 車種名キーワード
       → `carSpec`（年式・走行距離・修復歴）が付いてくる。ただしタイトルに
         車種名がない出品は漏れる。車種リーフのカテゴリ ID で他車種を弾く。
    2. 車種リーフカテゴリを直接指定
       → そのカテゴリの全件が取れる（`carSpec` は付かない）。1 で漏れた分を補う。

    落札 ID でマージし、年式が取れたものを優先する。
    """
    wanted = set(vehicle.yahoo_categories)
    if not wanted:
        return []

    by_id: dict[str, dict[str, Any]] = {}

    # --- パス1: carSpec つき ---
    for item in _search(fetcher, {"auccat": USED_CAR_CATEGORY, "p": vehicle.name}):
        if (item.get("category") or {}).get("id") not in wanted:
            continue
        row = _normalize(item, vehicle, snapshot)
        if row:
            by_id[row["auction_id"]] = row
    with_spec = len(by_id)

    # --- パス2: カテゴリ全件で補完 ---
    for category_id in vehicle.yahoo_categories:
        for item in _search(fetcher, {"auccat": category_id}):
            row = _normalize(item, vehicle, snapshot)
            if not row:
                continue
            existing = by_id.get(row["auction_id"])
            # パス1 で年式つきが取れていればそちらを残す
            if existing and existing.get("model_year"):
                continue
            by_id[row["auction_id"]] = row

    rows = list(by_id.values())
    dated = sum(1 for r in rows if r.get("model_year"))
    log.info("  yahoo %s: %s件 (うち年式あり %s / キーワード一致 %s)", vehicle.name, len(rows), dated, with_spec)
    return rows
