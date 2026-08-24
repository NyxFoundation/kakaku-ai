"""ヤフオク! の**出品中**オークションを監視する。

落札側（`yahoo_auction`）が「終わった値段」を集めるのに対し、こちらは
「いま出ていて、まだ買えるもの」を追う。

出品中の一覧は `/carsearch` で、robots.txt に禁止行はない。落札検索と違って
**全件に `carSpec` が付いてくる**ので、年式・走行距離・修復歴が最初から揃う。

**URL の作り方に落とし穴がある。**
`/carsearch?auccat=<車種カテゴリ>` は auccat を**黙って無視**し、全メーカーの
全車種 59,000 件を返す。これに気づかず組んで、セレナのつもりで全車種の先頭6ページを
拾い、同じ出品が複数車種に重複して出ていた。

正しくは `/search/search?auccat=<車種カテゴリ>` を叩く。すると
`/carsearch?brand_id=<車種ID>` にリダイレクトされ、こちらは正しく絞られる。
ただし**リダイレクトで `b`（ページ送り）が落ちる**ので、
`brand_id` を一度取ってから `/carsearch?brand_id=...&b=...` で自前でページを送る。
解決した `brand_id` は `config/yahoo_brand_ids.json` に貯めて使い回す。

さらに `seller` が付く。ここが監視の肝で、

```jsonc
"seller": {
  "isStore": false,        // ★ 個人かストアかを Yahoo が明示している
  "isBestStore": false,
  "goodRating": "97.5%",   // 評価率
  "type": "AUCTION",
  "city": "福津市"          // 所在地
}
```

推測しなくてよい。実測ではセレナの1ページ目 50件中 49件が個人出品だった。

`mileageType` に `TAMPERED`（メーター改ざん・巻き戻し）が入ることがあるのも
ここで拾える。強い除外シグナルになる。

**説明文と画像の中身は一覧に入っていない。** 必要なら商品ページを個別に開く
（`yahoo_detail` と同じ経路）。新着ぶんだけなら現実的な回数で済む。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from ..http import Fetcher
from ..vehicles import CONFIG_DIR

log = logging.getLogger(__name__)

BASE = "https://auctions.yahoo.co.jp/carsearch"
RESOLVE_URL = "https://auctions.yahoo.co.jp/search/search"
BRAND_ID_PATH = CONFIG_DIR / "yahoo_brand_ids.json"
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
PAGE_SIZE = 50
MAX_PAGES = 6  # 出品中は 1車種 1000件超のこともある。新着監視なので先頭数ページで足りる

# robots.txt が /search/*?*<param>= で禁じているもの。carsearch へは
# リダイレクトされるだけなので、同じ制約を守っておく。
FORBIDDEN_PARAMS = frozenset(
    {
        "pstagefree", "new", "offer", "shipping", "istatus", "abatch",
        "loc_cd", "type", "mode", "n", "s1", "o1", "max_sprice",
    }
)


def _check_params(params: dict[str, Any]) -> None:
    bad = FORBIDDEN_PARAMS & set(params)
    if bad:
        raise ValueError(f"robots.txt で禁止されたパラメータです: {sorted(bad)}")


def _model_year_month(model_date: Any) -> int | None:
    if not model_date:
        return None
    s = str(model_date)
    if len(s) < 6:
        return None
    try:
        ym = int(s[:6])
    except ValueError:
        return None
    return ym if 190001 <= ym <= 210012 else None


def _load_brand_ids() -> dict[str, int]:
    if BRAND_ID_PATH.exists():
        return {k: int(v) for k, v in json.loads(BRAND_ID_PATH.read_text(encoding="utf-8")).items()}
    return {}


def _save_brand_ids(table: dict[str, int]) -> None:
    BRAND_ID_PATH.write_text(
        json.dumps({k: table[k] for k in sorted(table)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def resolve_brand_id(fetcher: Fetcher, category_id: int) -> int | None:
    """車種カテゴリ ID → carsearch の brand_id。結果は設定に貯める。

    `/search/search?auccat=...` が `/carsearch?brand_id=...` にリダイレクトするので、
    最終 URL から拾う。
    """
    table = _load_brand_ids()
    key = str(category_id)
    if key in table:
        return table[key]

    params = {"auccat": category_id}
    _check_params(params)
    resp = fetcher.session.get(RESOLVE_URL, params=params, timeout=45, allow_redirects=True)
    m = re.search(r"brand_id=(\d+)", resp.url)
    if not m:
        log.warning("  yahoo出品中: brand_id を解決できません (auccat=%s url=%s)", category_id, resp.url)
        return None
    table[key] = int(m.group(1))
    _save_brand_ids(table)
    log.info("  yahoo出品中: auccat=%s -> brand_id=%s を記録", category_id, table[key])
    return table[key]


def _search(fetcher: Fetcher, brand_id: int) -> Iterator[dict[str, Any]]:
    total: int | None = None
    for page in range(MAX_PAGES):
        params = {"brand_id": brand_id, "b": page * PAGE_SIZE + 1}
        _check_params(params)
        m = NEXT_DATA.search(fetcher.get_text(BASE, params))
        if not m:
            log.warning("  yahoo出品中: __NEXT_DATA__ が無い (brand_id=%s)", brand_id)
            return
        listing = json.loads(m.group(1))["props"]["pageProps"]["initialState"]["search"][
            "items"
        ]["listing"]
        if total is None:
            total = listing.get("totalResultsAvailable", 0)
        items = listing.get("items") or []
        if not items:
            return
        yield from items
        if (page + 1) * PAGE_SIZE >= (total or 0):
            return


def collect(fetcher: Fetcher, vehicle, snapshot: str) -> list[dict[str, Any]]:
    """1車種ぶんの出品中オークションを正規化して返す。"""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    wanted = set(vehicle.yahoo_categories)
    for category_id in vehicle.yahoo_categories:
        brand_id = resolve_brand_id(fetcher, category_id)
        if brand_id is None:
            continue
        for item in _search(fetcher, brand_id):
            # brand_id が期待の車種を指しているか、返ってきた側でも確かめる
            if (item.get("category") or {}).get("id") not in wanted:
                continue
            auction_id = item.get("auctionId")
            if not auction_id or auction_id in seen:
                continue
            seen.add(auction_id)

            spec = item.get("carSpec") or {}
            seller = item.get("seller") or {}
            ym = _model_year_month(spec.get("modelDate"))
            rating = seller.get("goodRating") or ""

            rows.append(
                {
                    "snapshot_date": snapshot,
                    "source": "yahoo_open",
                    "vehicle_key": vehicle.key,
                    "vehicle_name": vehicle.name,
                    "maker": vehicle.maker,
                    "auction_id": auction_id,
                    "title": item.get("title", ""),
                    "current_price": item.get("price"),
                    "buy_now_price": item.get("buyNowPrice"),
                    "overhead_costs": spec.get("overheadCosts"),
                    "bid_count": item.get("bidCount"),
                    "end_time": item.get("endTime"),
                    "start_time": item.get("startTime"),
                    "model_year_month": ym,
                    "model_year": ym // 100 if ym else None,
                    "generation": vehicle.generation_for_model_year(ym // 100 if ym else None),
                    "mileage_km": spec.get("mileage"),
                    "mileage_type": spec.get("mileageType"),
                    "repair_type": spec.get("repairType"),
                    # --- 出品者 ---
                    "seller_is_store": bool(seller.get("isStore")),
                    "seller_is_best_store": bool(seller.get("isBestStore")),
                    "seller_rating": rating,
                    "seller_rating_pct": _pct(rating),
                    "seller_city": seller.get("city"),
                    "seller_id": seller.get("userId"),
                    "is_fixed_price": item.get("isFixedPrice"),
                    "image_url": item.get("imageUrl"),
                    "url": f"https://page.auctions.yahoo.co.jp/jp/auction/{auction_id}",
                }
            )

    private = sum(1 for r in rows if not r["seller_is_store"])
    log.info("  yahoo出品中 %s: %s件（個人 %s / ストア %s）",
             vehicle.name, len(rows), private, len(rows) - private)
    return rows


def _pct(rating: str) -> float | None:
    m = re.search(r"([\d.]+)\s*%", rating or "")
    return float(m.group(1)) if m else None
