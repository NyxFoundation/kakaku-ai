"""カーセンサーの在庫を**個体単位**で追いかけて、店頭でいくらで売れたかを推定する。

ヤフオクは落札価格＝成約値がそのまま取れる。一方で**店頭の成約価格はどこにも公開
されていない**。カーセンサーもグーネットもジモティーも「売り値（掲載価格）」までで、
いくらで売れたかは出てこない。

そこで、在庫一覧を毎週撮って個体を追う。カーセンサーの各在庫には
`AU7154621207` のような安定した ID が付いているので、

* 先週まで載っていた ID が今週消えた → 市場から出た（＝おおむね売れた）
* そのときの最後の掲載価格 → **成約価格の上限の目安**
* 初出から消えるまでの週数 → **在庫回転の速さ**

が出せる。`data/carsensor_listings.jsonl` に個体ごとの初出・最終確認・価格推移を
持ち、毎週更新する。

**これは推定であって成約価格そのものではない。** 注意点:

* 掲載終了は「売れた」以外に、取り下げ・掲載期限切れ・値段を変えての再掲載でも起きる。
  再掲載は ID が変わることがあるので、その場合は「消えて新しく出た」ように見える。
* 実際の成約額は値引き交渉ぶん掲載価格より下がるのが普通。だから上限の目安。

一覧ページ `/usedcar/b<メーカー>/s<車種>/index{N}.html` は robots.txt の
Disallow に入っていない（禁止は `/usedcar/search.php` や問い合わせ系）。
`usedcar-detail-index.xml` というサイトマップも公開されている。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup

from ..aggregate import inspection_year_month
from ..http import Fetcher
from ..vehicles import DATA_DIR

log = logging.getLogger(__name__)

BASE = "https://www.carsensor.net/usedcar/b{maker}/s{model}/index{page}.html"
PER_PAGE = 30
MAX_PAGES_PER_YEAR = 2  # 1年式あたり最大 60 件。週次の実行時間とのつり合いでこの辺り
STORE_PATH = DATA_DIR / "carsensor_listings.jsonl"

CARD = "div.cassette[id$=\"_cas\"]"


def _num(text: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", text.replace(",", "").replace(" ", ""))
    return float(m.group(0)) if m else None


def _price_manyen(card, cls: str) -> float | None:
    """`支払総額 339 .8 万円` のように整数部と小数部が別要素に分かれている。"""
    main = card.select_one(f".{cls}__mainPriceNum")
    sub = card.select_one(f".{cls}__subPriceNum")
    if not main:
        return None
    whole = _num(main.get_text())
    if whole is None:
        return None
    frac = (sub.get_text(strip=True) if sub else "") or ""
    m = re.search(r"\.\d+", frac)
    return float(f"{int(whole)}{m.group(0)}") if m else whole


def _spec(card) -> dict[str, str]:
    """`年式 2018 (H30) 走行距離 6.7 万km …` の並びを辞書にする。"""
    out: dict[str, str] = {}
    for box in card.select(".specList__detailBox"):
        title = box.select_one(".specList__title")
        data = box.select_one(".specList__data")
        if title and data:
            out[re.sub(r"\s+", "", title.get_text())] = re.sub(
                r"\s+", " ", data.get_text(" ", strip=True)
            )
    return out


def _parse_card(card, vehicle, code: str) -> dict[str, Any] | None:
    listing_id = (card.get("id") or "").replace("_cas", "")
    if not listing_id:
        return None

    spec = _spec(card)
    year = None
    m = re.search(r"(\d{4})", spec.get("年式", ""))
    if m:
        year = int(m.group(1))

    mileage_km = None
    m = re.search(r"([\d.]+)\s*万km", spec.get("走行距離", ""))
    if m:
        mileage_km = int(float(m.group(1)) * 10_000)
    elif "走行距離" in spec:
        m = re.search(r"([\d,]+)\s*km", spec["走行距離"])
        if m:
            mileage_km = int(m.group(1).replace(",", ""))

    # グレード＋装備が全部つながった長い文字列。頭のほうにグレードが来る
    title = card.select_one(".cassetteMain__title")

    return {
        "listing_id": listing_id,
        "vehicle_key": vehicle.key,
        "vehicle_name": vehicle.name,
        "title": re.sub(r"\s+", " ", title.get_text(" ", strip=True))[:160] if title else "",
        "carsensor_code": code,
        "model_year": year,
        "generation": vehicle.generation_for_model_year(year),
        "total_price_manyen": _price_manyen(card, "totalPrice"),
        "base_price_manyen": _price_manyen(card, "basePrice"),
        "mileage_km": mileage_km,
        "inspection": spec.get("車検", ""),
        "inspection_ym": inspection_year_month(spec.get("車検")),
        "repair_history": spec.get("修復歴", ""),
        "warranty": spec.get("保証", ""),
        "url": f"https://www.carsensor.net/usedcar/detail/{listing_id}/index.html",
    }


def _fetch_year(
    fetcher: Fetcher, vehicle, code: str, year: int
) -> list[dict[str, Any]]:
    maker, model = code.split("_S")
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES_PER_YEAR + 1):
        url = BASE.format(maker=maker, model=f"{int(model):03d}", page="" if page == 1 else page)
        try:
            html = fetcher.get_text(url, {"YMIN": year, "YMAX": year})
        except Exception as exc:  # noqa: BLE001
            if "404" not in str(exc):
                log.warning("  carsensor在庫 %s %s p%s: %s", vehicle.name, year, page, exc)
            break
        cards = BeautifulSoup(html, "lxml").select(CARD)
        if not cards:
            break
        for card in cards:
            parsed = _parse_card(card, vehicle, code)
            # YMIN/YMAX で絞っているが、念のため年式を確認する
            if parsed and parsed["model_year"] == year:
                rows.append(parsed)
        if len(cards) < PER_PAGE:
            break
    return rows


# ------------------------------------------------------------------ 永続ストア


def load_store() -> dict[str, dict[str, Any]]:
    if not STORE_PATH.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in STORE_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["listing_id"]] = row
    return out


def save_store(store: dict[str, dict[str, Any]]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STORE_PATH.open("w", encoding="utf-8") as fh:
        for listing_id in sorted(store):
            fh.write(json.dumps(store[listing_id], ensure_ascii=False) + "\n")


def track(
    fetcher: Fetcher,
    vehicles: Iterable[Any],
    snapshot: str,
    *,
    model_year_from: int,
    model_year_to: int,
) -> dict[str, int]:
    """今週の在庫を撮って、先週から消えた個体に「掲載終了」の印をつける。

    戻り値は件数のサマリ。実際の結果（成約推定）は 2 回目の実行から出る。
    """
    store = load_store()
    seen_now: set[str] = set()

    for vehicle in vehicles:
        for code in vehicle.carsensor_codes:
            for year in range(model_year_from, model_year_to + 1):
                for row in _fetch_year(fetcher, vehicle, code, year):
                    listing_id = row["listing_id"]
                    seen_now.add(listing_id)
                    existing = store.get(listing_id)
                    if existing is None:
                        store[listing_id] = {
                            **row,
                            "first_seen": snapshot,
                            "last_seen": snapshot,
                            "price_history": [[snapshot, row["total_price_manyen"]]],
                            "delisted_on": None,
                        }
                        continue
                    existing.update(
                        {k: v for k, v in row.items() if v is not None}
                    )
                    existing["last_seen"] = snapshot
                    # 値下げがあれば記録する。売れる直前に下げる例が多いので後で効く。
                    history = existing.setdefault("price_history", [])
                    if not history or history[-1][1] != row["total_price_manyen"]:
                        history.append([snapshot, row["total_price_manyen"]])
                    # 出戻り（再掲載）なら掲載終了を取り消す
                    existing["delisted_on"] = None

    # 前回まで見えていて今週見えないものを「掲載終了」にする
    newly_gone = 0
    for listing_id, row in store.items():
        if listing_id in seen_now or row.get("delisted_on"):
            continue
        if row.get("last_seen") == snapshot:
            continue
        row["delisted_on"] = snapshot
        newly_gone += 1

    save_store(store)
    counts = {
        "tracked": len(store),
        "seen_this_week": len(seen_now),
        "newly_delisted": newly_gone,
    }
    log.info("  carsensor在庫: 追跡 %s件 / 今週 %s件 / 今週消えた %s件", *counts.values())
    return counts


def delisted_rows(snapshot: str) -> list[dict[str, Any]]:
    """掲載が消えた個体＝成約したとみられるものを書き出し用に並べる。"""
    out: list[dict[str, Any]] = []
    for row in load_store().values():
        if not row.get("delisted_on"):
            continue
        history = row.get("price_history") or []
        first_price = history[0][1] if history else None
        last_price = history[-1][1] if history else None
        out.append(
            {
                "snapshot_date": snapshot,
                "vehicle_name": row["vehicle_name"],
                "vehicle_key": row["vehicle_key"],
                "model_year": row["model_year"],
                "generation": row["generation"],
                "listing_id": row["listing_id"],
                "first_seen": row["first_seen"],
                "delisted_on": row["delisted_on"],
                "first_price_manyen": first_price,
                "last_price_manyen": last_price,
                "price_cut_manyen": (
                    round(first_price - last_price, 1)
                    if first_price is not None and last_price is not None
                    else None
                ),
                "base_price_manyen": row.get("base_price_manyen"),
                "mileage_km": row.get("mileage_km"),
                "inspection": row.get("inspection"),
                "inspection_ym": row.get("inspection_ym"),
                "repair_history": row.get("repair_history"),
                "url": row["url"],
            }
        )
    out.sort(key=lambda r: (r["delisted_on"], r["vehicle_name"], -(r["model_year"] or 0)))
    return out
