"""**旧車**（既定 1988〜2001年式）の在庫を全車種ぶん集めて、状態の良い順に並べる。

深掘り20車種でも `wide` でもカバーできない領域。理由は 2 つある。

* `wide` が読むカーセンサーの相場ページは、年式の列が直近十数年しか無い。
  実際 2026-08-26 の取得だと **2012年以前は 1 本の「それ以前」バケット**に
  潰れていて、90年代の相場は出せない。
* 旧車は同じ車種でも個体差が価格を支配する。年式別の中央値より、
  1 台ずつの走行距離・修復歴・整備履歴を見るほうが意味がある。

なので在庫ページを直接舐める。`YMIN`/`YMAX` に**範囲**を渡せるので、
1 車種 1 リクエストで「その車種の旧車在庫が何台あるか」が分かる。
在庫ゼロの車種はそこで打ち切りになるため、2,200 車種を全部見ても
実質 2,400 回程度で済む。curation で車種を選ばずに済むのが大きい。

### 並べ方

古い車は「安い個体」ではなく「程度の良い個体」を選ぶべきなので、価格では並べない。

* **走行距離** — 同年式のなかで少ないほど加点。旧車ではここがいちばん効く
* **修復歴なし** — 加点。「あり」は大きく減点
* **車検残** — 長いほど加点（残がないと十数万の出費になる）
* **保証・整備** — 付いていれば加点
* **価格** — 同年式の中央値より安ければ少し加点。ただし**安すぎは減点**。
  30年落ちで極端に安い個体は、それなりの理由があることが多い

デザインは数値化できないので触らない。グレードと装備の載った出品タイトルを
そのまま並記して、目で見て判断してもらう。
"""

from __future__ import annotations

import logging
import statistics as st
from typing import Any, Iterable

from .aggregate import months_until
from .http import Fetcher
from .sources import carsensor_listings as cl
from .wide import load_catalog

log = logging.getLogger(__name__)

YEAR_FROM = 1988
YEAR_TO = 2001


class _Vehicle:
    """`carsensor_listings` に渡すための最小限の車種。世代は持たない。"""

    def __init__(self, code: str, name: str) -> None:
        self.key = code
        self.name = name
        self.carsensor_codes = (code,)

    @staticmethod
    def generation_for_model_year(_year: int | None) -> str:
        return ""


def score(
    row: dict[str, Any],
    peer_price: float | None,
    peer_mileage: float | None,
) -> tuple[float, list[str]]:
    """状態の良さを点数にする。理由も返す（数字だけでは判断できないので）。"""
    points = 0.0
    why: list[str] = []

    mileage = row.get("mileage_km")
    if mileage is not None and peer_mileage:
        ratio = mileage / peer_mileage
        if ratio <= 0.5:
            points += 3
            why.append(f"同年式の半分以下の走行（{mileage / 10000:.1f}万km）")
        elif ratio <= 0.8:
            points += 1.5
            why.append(f"走行が少なめ（{mileage / 10000:.1f}万km）")
        elif ratio >= 1.5:
            points -= 1.5
            why.append(f"走行が多め（{mileage / 10000:.1f}万km）")

    repair = (row.get("repair_history") or "").strip()
    if repair == "なし":
        points += 2
        why.append("修復歴なし")
    elif repair == "あり":
        points -= 4
        why.append("修復歴あり")

    ym = row.get("inspection_ym") or 0
    left = months_until(ym) or 0
    if left > 0:
        points += min(left / 12, 2)
        why.append(f"車検 {ym // 100}/{ym % 100:02d} まで（残り{left}ヶ月）")
    elif (row.get("inspection") or "") == "車検整備付":
        points += 1
        why.append("車検整備付")

    # 「保証付」と「保証無」の2値。部分一致だと後者も拾ってしまう
    if (row.get("warranty") or "") == "保証付":
        points += 1
        why.append("保証付")

    price = row.get("total_price_manyen") or row.get("base_price_manyen")
    if price and peer_price:
        ratio = price / peer_price
        if 0.6 <= ratio <= 0.9:
            points += 1
            why.append(f"同年式より安い（中央値 {peer_price:.0f}万）")
        elif ratio < 0.5:
            # 30年落ちで極端に安いのは、それなりの理由があることが多い
            points -= 1
            why.append("同年式より極端に安い（要確認）")
    return points, why


def rescore(rows: list[dict[str, Any]]) -> None:
    """**車種 × 年式**のなかで相対評価してスコアを振り直す（その場で書き換える）。

    比較相手は同じ車種の同じ年式に限る。240 の走行 8万km と V70 の 8万km は
    意味がまるで違うので、車種をまたいで中央値を取ると評価が壊れる。
    """
    peers: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        peers.setdefault((row.get("carsensor_code") or "", row.get("model_year") or 0), []).append(row)

    for key, group in peers.items():
        prices = [r["total_price_manyen"] for r in group if r.get("total_price_manyen")]
        mileages = [r["mileage_km"] for r in group if r.get("mileage_km")]
        # 1 台しかない年式は比較相手がいない。中央値＝自分になって
        # 「相場どおり」としか言えないので、比較は諦めて素点だけ付ける
        med_price = st.median(prices) if len(prices) >= 2 else None
        med_mileage = st.median(mileages) if len(mileages) >= 2 else None
        for row in group:
            row["score"], row["why"] = score(row, med_price, med_mileage)
            row["peer_count"] = len(group)


def crawl(
    fetcher: Fetcher,
    *,
    year_from: int = YEAR_FROM,
    year_to: int = YEAR_TO,
    makers: Iterable[str] | None = None,
    limit: int | None = None,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """カタログの全車種を旧車の年式レンジで舐めて、拾えた在庫を全部返す。"""
    catalog = load_catalog()
    if not catalog:
        raise RuntimeError("カタログが空です。先に `kakaku-ai wide` を実行してください。")

    codes = sorted(catalog)
    if makers:
        wanted = set(makers)
        codes = [c for c in codes if (catalog[c].get("maker") or "") in wanted]
    if limit:
        codes = codes[:limit]
    log.info("旧車クロール: %s車種 × %s〜%s年式", len(codes), year_from, year_to)

    rows: list[dict[str, Any]] = []
    truncated: list[str] = []
    with_stock = 0
    for i, code in enumerate(codes, 1):
        meta = catalog[code]
        vehicle = _Vehicle(code, meta.get("model_name") or code)
        try:
            found = cl.fetch_range(fetcher, vehicle, code, year_from, year_to, max_pages=max_pages)
        except Exception as exc:  # noqa: BLE001 - 1車種のこけで全体を止めない
            log.warning("  %s: %s", code, exc)
            continue
        if not found:
            continue
        with_stock += 1
        for row in found:
            row.update({
                "model_name": meta.get("model_name") or code,
                "maker": meta.get("maker"),
                "origin": meta.get("origin"),
                "body_type": meta.get("body_type"),
                "production_period": meta.get("production_period"),
            })
        rows.extend(found)
        if len(found) >= max_pages * cl.PER_PAGE:
            truncated.append(vehicle.name)
        log.info("  [%s/%s] %-24s %s台", i, len(codes), vehicle.name, len(found))

    if truncated:
        # 黙って切り捨てると「全部入っている」と誤解したまま台数を数えることになる
        log.warning("  ページ上限(%s)で打ち切った車種 %s: %s。--max-pages を上げると全部入る",
                    max_pages, len(truncated), "・".join(truncated))

    rows = dedupe(rows)
    rescore(rows)
    log.info("旧車クロール完了: 在庫のある車種 %s / 個体 %s台", with_stock, len(rows))
    return rows


def dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同じ掲載を 1 件にまとめる（先に出たほうを残す）。"""
    seen: set[str] = set()
    out = []
    for row in rows:
        listing_id = row.get("listing_id")
        if not listing_id or listing_id in seen:
            continue
        seen.add(listing_id)
        out.append(row)
    return out


def reapply_catalog(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保存済みの在庫にカタログの車種情報を貼り直す（ネットワークなし）。

    メーカーやボディタイプは取得時にカタログから焼き込んでいるので、
    カタログ側を直しても既存の行には反映されない。分類を直したあと
    取り直さずに済むよう、ここで貼り替えてスコアも振り直す。
    """
    catalog = load_catalog()
    for row in rows:
        meta = catalog.get(row.get("carsensor_code") or "")
        if not meta:
            continue
        row.update({
            "model_name": meta.get("model_name") or row.get("model_name"),
            "maker": meta.get("maker"),
            "origin": meta.get("origin"),
            "body_type": meta.get("body_type"),
            "production_period": meta.get("production_period"),
        })
    rows = dedupe(rows)
    rescore(rows)
    return rows


def by_model(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """車種ごとに台数・価格帯・走行距離をまとめる。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get("carsensor_code") or "", []).append(row)

    out: list[dict[str, Any]] = []
    for code, group in groups.items():
        head = group[0]
        prices = sorted(r["total_price_manyen"] for r in group if r.get("total_price_manyen"))
        mileages = sorted(r["mileage_km"] for r in group if r.get("mileage_km"))
        years = [r["model_year"] for r in group if r.get("model_year")]
        out.append({
            "maker": head.get("maker"),
            "origin": head.get("origin"),
            "body_type": head.get("body_type"),
            "model_name": head.get("model_name"),
            "carsensor_code": code,
            "production_period": head.get("production_period"),
            "listing_count": len(group),
            "year_min": min(years) if years else None,
            "year_max": max(years) if years else None,
            "price_min_manyen": prices[0] if prices else None,
            "price_median_manyen": round(st.median(prices), 1) if prices else None,
            "price_max_manyen": prices[-1] if prices else None,
            "price_unknown_n": len(group) - len(prices),
            "mileage_median_mankm": round(st.median(mileages) / 10000, 1) if mileages else None,
            "repaired_n": sum(1 for r in group if r.get("repair_history") == "あり"),
            "best_score": round(max(r.get("score") or 0 for r in group), 1),
            "url": f"https://www.carsensor.net/usedcar/b{code.split('_S')[0]}"
                   f"/s{int(code.split('_S')[1]):03d}/index.html",
        })
    out.sort(key=lambda r: -r["listing_count"])
    return out
