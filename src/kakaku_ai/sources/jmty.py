"""ジモティーの中古車の**掲載価格**を取る。

`https://jmty.jp/all/car-<メーカー>/g-2310`（トヨタ アルファード）のように車種カテゴリがある。
一覧ページの 1 件に価格・走行距離・年式・車種・地域が構造化されて載っているので、
明細ページを開かなくても集計できる。
robots.txt は `User-agent: * / Allow: /` で全面的に許可されている。

**これは「個人売買の相場」ではない。** 最初はそのつもりで入れたが、実測したら違った。

* 大半が **提携サイト（中古車販売店の在庫フィード）**。アルファードは 395 件中 388 件。
* 提携タグの付かない直接投稿も、31% がタイトルに「自社ローン」「総額」「保証」
  「納車」といった業者ワードを含む。つまり `is_alliance` が false でも個人とは限らない。
* 同じ車を何度も投稿する例がある（ウィッシュで 1 台が 8 投稿）。
* **掲載価格であって成約価格ではない。** 売れた投稿がすぐ消えないので、
  古い出品が残って相場を上に引っ張る。

そのため本モジュールが出すのは「業者・個人混在の掲載価格」であり、
ヤフオクの落札（成約値）とは性質が違う。カーセンサーの店頭価格に近いが、
母集団も掲載の作法も別なので、独立した参考系列として置いている。

同一の (価格, 年式, 走行距離) は 1 台とみなして重複を落とす。
一覧には他カテゴリの PR 枠（`p-item-pr`）も差し込まれる（トヨタの一覧に
マツダの車が出てくる）ので必ず落とす。キーワード検索は本文にも当たるため、
車種カテゴリかタイトルで車種が一致するものだけを残す。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from ..http import Fetcher

log = logging.getLogger(__name__)

CATEGORY_URL = "https://jmty.jp/all/car-{maker}/{category}"
KEYWORD_URL = "https://jmty.jp/all/car-{maker}"
DEFAULT_PAGES = 6  # 1ページ 50件（+ PR枠。PR は落とす）

MANYEN = 10_000
# 「0円」「0km」「0年」は未記入の意味で入っているので数値として扱わない
MIN_PRICE_YEN = 10_000


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def _int(s: str) -> int | None:
    m = re.search(r"\d[\d,]*", s)
    return int(m.group(0).replace(",", "")) if m else None


# タイトルに出てきたら業者投稿とみなす語。個人かどうかの判定は完全にはできないので、
# 「業者らしさ」のフラグとして持つだけで、集計からは外さない。
DEALER_HINTS = ("自社ローン", "総額", "保証付", "納車", "当店", "審査", "在庫", "販売店", "buy")


def _parse_item(li, vehicle, snapshot: str) -> dict[str, Any] | None:
    classes = " ".join(li.get("class") or [])
    if "p-item-pr" in classes or li.select_one(".p-item-pr-icon"):
        return None  # 他カテゴリの広告枠

    title_el = li.select_one(".p-item-title a")
    if not title_el:
        return None

    important = _text(li.select_one(".p-item-most-important"))
    # 例: 「2,450,000円 165,500km 2007年」
    price = None
    m = re.search(r"([\d,]+)\s*円", important)
    if m:
        price = int(m.group(1).replace(",", ""))
    mileage = None
    m = re.search(r"([\d,]+)\s*km", important)
    if m:
        mileage = int(m.group(1).replace(",", "")) or None
    year = None
    m = re.search(r"(\d{4})\s*年", important)
    if m and 1950 <= int(m.group(1)) <= 2100:
        year = int(m.group(1))

    if not price or price < MIN_PRICE_YEN:
        return None

    # 車種カテゴリ（/all/car-toy/g-NNNN）だけを拾う。地域リンクにも /g- が入るので絞る。
    model = ""
    for a in li.select(".p-item-supplementary-info a"):
        href = a.get("href") or ""
        if re.fullmatch(r"/all/car-[a-z]+/g-\d+", href):
            model = a.get_text(strip=True)
            break

    url = title_el.get("href") or ""
    if url.startswith("/"):
        url = "https://jmty.jp" + url

    return {
        "snapshot_date": snapshot,
        "source": "jmty",
        "vehicle_key": vehicle.key,
        "vehicle_name": vehicle.name,
        "title": _text(title_el),
        "url": url,
        "asking_price": price,
        "mileage_km": mileage,
        "model_year": year,
        "generation": vehicle.generation_for_model_year(year),
        "jmty_model": model,
        "region": _text(li.select_one(".p-item-secondary-important")),
        # 提携サイト = 中古車販売店の在庫フィード
        "is_alliance": bool(li.select_one(".p-item-alliance-tag")),
        # 提携タグが無くても業者の直接投稿は多い。目安のフラグ。
        "looks_like_dealer": any(k in _text(title_el) for k in DEALER_HINTS),
    }


def collect(
    fetcher: Fetcher,
    vehicle,
    snapshot: str,
    pages: int = DEFAULT_PAGES,
    model_year_from: int | None = None,
) -> list[dict[str, Any]]:
    """一覧を数ページ舐めて、掲載中の車を拾う。

    `model_year_from` を渡すと `model_year[min]` で絞る。対象外の古い年式で
    ページが埋まるのを防げる。年式未記入の投稿（個人出品に多い）は落ちるが、
    そもそも年式別集計には使えないので実害はない。
    """
    category = getattr(vehicle, "jmty_category", None)
    keyword = getattr(vehicle, "jmty_keyword", None)
    if not category and not keyword:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for page in range(1, pages + 1):
        params: dict[str, Any] = {}
        if category:
            url = CATEGORY_URL.format(maker=vehicle.jmty_maker, category=category)
        else:
            url = KEYWORD_URL.format(maker=vehicle.jmty_maker)
            params["keyword"] = keyword
        if model_year_from:
            params["model_year[min]"] = model_year_from
        if page > 1:
            params["page"] = page

        try:
            soup = BeautifulSoup(fetcher.get_text(url, params or None), "lxml")
        except Exception as exc:  # noqa: BLE001 - 1車種のこけで全体を止めない
            # 小さいカテゴリは最終ページの次が 404 になる。これは異常ではない。
            if "404" not in str(exc):
                log.warning("  jmty %s p%s: %s", vehicle.name, page, exc)
            break

        items = soup.select("li.p-articles-list-item")
        if not items:
            break

        new_on_page = 0
        for li in items:
            parsed = _parse_item(li, vehicle, snapshot)
            if not parsed or parsed["url"] in seen:
                continue
            if not _matches_vehicle(parsed, vehicle, bool(category)):
                continue
            seen.add(parsed["url"])
            rows.append(parsed)
            new_on_page += 1
        if new_on_page == 0:
            break

    rows = _dedupe(rows)
    direct = sum(1 for r in rows if not r["is_alliance"])
    dealerish = sum(1 for r in rows if r["is_alliance"] or r["looks_like_dealer"])
    log.info(
        "  jmty %s: %s件（提携 %s / 直接投稿 %s、うち業者ワードあり込みで %s が業者らしい）",
        vehicle.name, len(rows), len(rows) - direct, direct, dealerish,
    )
    return rows


def _matches_vehicle(row: dict[str, Any], vehicle, has_category: bool) -> bool:
    """その車種のものか確かめる。

    カテゴリ指定なら jmty 側が絞ってくれている。キーワード検索は本文にも当たるので
    （「プリウスα」で検索してヴォクシーが出た）、タイトルで車種名を確かめる。
    """
    pattern = getattr(vehicle, "jmty_title_pattern", None)
    if pattern:
        # カテゴリが車種より粗いことがある（三菱の「デリカ」は D:5 / D:2 / ミニ が同居）。
        # パターンが指定されていればカテゴリ指定でも必ずタイトルで確かめる。
        return bool(re.search(pattern, row["title"]))
    if has_category:
        return True
    return bool(
        re.search(
            re.escape(getattr(vehicle, "jmty_keyword", None) or vehicle.name), row["title"]
        )
    )


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同じ車の多重投稿を 1 件に落とす。

    ジモティーでは 1 台を文言だけ変えて何度も投稿する業者がいる
    （ウィッシュで 1 台が 8 投稿）。価格・年式・走行距離が揃えば同一車とみなす。
    """
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row["asking_price"], row["model_year"], row["mileage_km"])
        if all(k is not None for k in key):
            if key in seen:
                continue
            seen.add(key)
        out.append(row)
    return out


def by_year(rows: list[dict[str, Any]], vehicle, snapshot: str) -> list[dict[str, Any]]:
    """年式ごとの掲載価格を畳む。業者・個人は分けずに全部入れる。

    直接投稿だけを取り出しても「個人」にはならない（31% が業者ワード入り）ので、
    分けても意味のある系列にならない。代わりに `jmty_direct_n` で
    提携フィード以外が何件かを持たせて、内訳が見えるようにしてある。
    """
    buckets: dict[int, dict[str, Any]] = {}
    for row in rows:
        year = row.get("model_year")
        if not year:
            continue
        slot = buckets.setdefault(year, {"prices": [], "direct": 0})
        slot["prices"].append(row["asking_price"] / MANYEN)
        if not row["is_alliance"]:
            slot["direct"] += 1

    out: list[dict[str, Any]] = []
    for year in sorted(buckets):
        prices = sorted(buckets[year]["prices"])
        if not prices:
            continue
        mid = len(prices) // 2
        median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
        out.append(
            {
                "snapshot_date": snapshot,
                "source": "jmty",
                "vehicle_key": vehicle.key,
                "vehicle_name": vehicle.name,
                "model_year": year,
                "generation": vehicle.generation_for_model_year(year),
                "jmty_n": len(prices),
                "jmty_min_manyen": round(prices[0], 1),
                "jmty_median_manyen": round(median, 1),
                "jmty_max_manyen": round(prices[-1], 1),
                "jmty_direct_n": buckets[year]["direct"],
            }
        )
    return out
