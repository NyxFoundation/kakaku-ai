"""ヤフオク「中古車・新車」ノードの落札を**全部**さらう。

車種ごとにキーワードで引く `yahoo_auction` とは別系統。あちらは深掘り20車種の
相場を作るためのもので、こちらは「180日ぶんの落札を丸ごと持ってきて、
あとから好きな条件で切る」ためのもの。旧車のように車種が数百に散らばる用途では
車種ごとに引いていられない。

### なぜこうなるか

* `carSpec`（年式・走行距離・修復歴）が付くのは **`auccat=26360`（中古車・新車
  ノードそのもの）で検索したときだけ**。メーカーカテゴリ（例 日産 =
  2084007644）や車種カテゴリに降りると付かなくなる。実測で確認済み。
* `brand_id` は落札検索では 404。
* 年式で絞るクエリパラメータは存在しない（`__NEXT_DATA__` を漁っても無い）。

つまり「26360 を全部取ってきて手元で年式を見る」しか無い。ところが

* 180日ぶんの落札は **30,381件**（2026-08-26 時点）
* ページングは **b=15,001 あたりで打ち切られる**（b=16,001 は 0 件）

ので、1 回のソート順では半分しか取れない。そこで**並び順を変えて複数回**
さらい、`auctionId` で名寄せする。終了日時の昇順と降順は 180日窓の両端から
掘るので重複がほとんど無く、2 回でほぼ全部埋まる。価格順を足して残りを拾う。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from ..http import Fetcher

log = logging.getLogger(__name__)

BASE = "https://auctions.yahoo.co.jp/closedsearch/closedsearch"
USED_CAR_CATEGORY = "26360"
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

PAGE_SIZE = 50
# b=15,001 までは返ってくるが 16,001 は 0 件。1 ソートあたりの実質上限
MAX_B = 15_001

# 終了日時の昇順・降順で窓の両端から掘り、残りを価格順で拾う
SORTS: tuple[tuple[str, str], ...] = (
    ("end", "d"),
    ("end", "a"),
    ("cbids", "a"),
    ("cbids", "d"),
)


def _parse(html: str) -> dict[str, Any]:
    data = json.loads(NEXT_DATA.search(html).group(1))  # type: ignore[union-attr]
    return data["props"]["pageProps"]["initialState"]["search"]["items"]["listing"]


def _model_year_month(model_date: Any) -> int | None:
    """carSpec.modelDate (19980401) -> 199804"""
    s = str(model_date or "")
    if len(s) < 6:
        return None
    try:
        ym = int(s[:6])
    except ValueError:
        return None
    return ym if 190001 <= ym <= 210012 else None


def _maker_and_model(item: dict[str, Any]) -> tuple[str | None, str | None]:
    """categoryPath から メーカー / 車種 を取る。

    [オークション, 自動車、オートバイ, 中古車・新車, 日産, デイズルークス]
    のように並ぶので、中古車・新車 の次がメーカー、その次が車種。
    出品者がカテゴリを選んでいなければ無い。
    """
    path = item.get("categoryPath") or []
    for i, node in enumerate(path):
        if str(node.get("id")) == USED_CAR_CATEGORY:
            after = path[i + 1:]
            maker = after[0].get("name") if len(after) >= 1 else None
            model = after[1].get("name") if len(after) >= 2 else None
            return maker, model
    return None, None


def normalize(item: dict[str, Any], snapshot: str) -> dict[str, Any] | None:
    price = item.get("price")
    auction_id = item.get("auctionId")
    if not price or not auction_id:
        return None

    spec = item.get("carSpec") or {}
    ym = _model_year_month(spec.get("modelDate"))
    maker, model = _maker_and_model(item)
    seller = item.get("seller") or {}
    return {
        "snapshot_date": snapshot,
        "source": "yahoo_auction",
        "auction_id": auction_id,
        "title": item.get("title", ""),
        "maker": maker,
        "model_name": model,
        "category_id": (item.get("category") or {}).get("id"),
        "category_name": (item.get("category") or {}).get("name"),
        "price": int(price),
        "buy_now_price": item.get("buyNowPrice"),
        "bid_count": item.get("bidCount"),
        "end_time": item.get("endTime"),
        "model_year_month": ym,
        "model_year": ym // 100 if ym else None,
        "mileage_km": spec.get("mileage"),
        "mileage_type": spec.get("mileageType"),
        "repair_type": spec.get("repairType"),
        "overhead_costs": spec.get("overheadCosts"),
        "prefecture_code": item.get("prefectureCode"),
        "is_fixed_price": item.get("isFixedPrice"),
        "seller_is_store": seller.get("isStore"),
        "image_url": item.get("image", {}).get("url") if isinstance(item.get("image"), dict) else None,
        "url": f"https://page.auctions.yahoo.co.jp/jp/auction/{auction_id}",
    }


def _sweep_one(fetcher: Fetcher, sort_key: str, order: str, max_b: int) -> Iterator[dict[str, Any]]:
    total: int | None = None
    b = 1
    while b <= max_b:
        listing = _parse(fetcher.get_text(
            BASE, {"auccat": USED_CAR_CATEGORY, "s1": sort_key, "o1": order, "b": b}
        ))
        if total is None:
            total = listing.get("totalResultsAvailable") or 0
            log.info("  ソート %s/%s: 全 %s件", sort_key, order, total)
        items = listing.get("items") or []
        if not items:
            return
        yield from items
        if b + PAGE_SIZE > total:
            return
        b += PAGE_SIZE


def sweep(
    fetcher: Fetcher,
    snapshot: str,
    *,
    sorts: tuple[tuple[str, str], ...] = SORTS,
    max_b: int = MAX_B,
    model_year_to: int | None = None,
) -> list[dict[str, Any]]:
    """中古車ノードの落札を並び順を変えて何度もさらい、名寄せして返す。

    `model_year_to` を渡すと、その年式以前だけ残す（旧車用）。ただし取得自体は
    全件に対して行う。年式で絞るクエリが無いので、手元で捨てるしかない。
    """
    pool: dict[str, dict[str, Any]] = {}
    total: int | None = None

    for sort_key, order in sorts:
        before = len(pool)
        for item in _sweep_one(fetcher, sort_key, order, max_b):
            row = normalize(item, snapshot)
            if row:
                pool.setdefault(row["auction_id"], row)
        log.info("  ソート %s/%s 完了: 新規 %s件（累計 %s件）",
                 sort_key, order, len(pool) - before, len(pool))
        # 全部拾えたらそれ以上のソートは無駄
        if total is None:
            total = _parse(fetcher.get_text(
                BASE, {"auccat": USED_CAR_CATEGORY}
            )).get("totalResultsAvailable")
        if total and len(pool) >= total:
            log.info("  全件そろったので打ち切り")
            break

    if total and len(pool) < total:
        log.warning("  ヤフオク: %s件中 %s件しか取れなかった（ページング上限 b=%s）",
                    total, len(pool), max_b)

    rows = list(pool.values())
    if model_year_to is not None:
        with_year = [r for r in rows if r.get("model_year")]
        rows = [r for r in with_year if r["model_year"] <= model_year_to]
        log.info("  年式 %s以前: %s件（年式が取れたもの %s件 / 全 %s件）",
                 model_year_to, len(rows), len(with_year), len(pool))
    return sorted(rows, key=lambda r: r.get("end_time") or "")
