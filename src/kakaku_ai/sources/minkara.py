"""みんカラのクルマレビューを年式つきで取る。

`https://minkara.carview.co.jp/car/<メーカー>/<slug>/review/` の 1 レビューに

* グレードと年式（`Z(CVT_2.5_ガソリン) (2025年)`）
* おすすめ度 + デザイン / 走行性能 / 乗り心地 / 積載性 / 燃費 / 価格
* 満足している点 / 不満な点 / 総評

が入っている。年式が取れるので**年式別の口コミ評価**まで落とせるのが効く。
"""

from __future__ import annotations

import logging
import re
import warnings
from typing import Any

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from ..http import Fetcher

# ページ末尾に XML 断片が混ざることがあり lxml が毎回警告を出すが、実害はない
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

log = logging.getLogger(__name__)

BASE = "https://minkara.carview.co.jp/car/{maker}/{slug}/review/"
DEFAULT_PAGES = 6  # 1ページ 5件。新しい順に 30 件ぶんを毎週サンプルする

AXES = {
    "デザイン": "design",
    "走行性能": "driving",
    "乗り心地": "ride",
    "積載性": "loading",
    "燃費": "fuel_economy",
    "価格": "price",
}


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"[ \t　]+", " ", re.sub(r"\s*\n\s*", " ", text)).strip()


def _num(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


def _parse_card(card, vehicle, snapshot: str, url: str) -> dict[str, Any] | None:
    title_el = card.select_one(".kr_ttltxt a")
    grade_el = card.select_one(".kr_reviewGrade")
    grade_text = _clean(grade_el.get_text() if grade_el else "")

    year = None
    m = re.search(r"\((\d{4})年\)", grade_text)
    if m:
        year = int(m.group(1))
    grade = re.sub(r"\(\d{4}年\)", "", grade_text).strip()

    row: dict[str, Any] = {
        "snapshot_date": snapshot,
        "source": "minkara",
        "vehicle_key": vehicle.key,
        "vehicle_name": vehicle.name,
        "review_title": _clean(title_el.get_text() if title_el else ""),
        "review_url": (
            "https://minkara.carview.co.jp" + title_el["href"]
            if title_el and title_el.get("href", "").startswith("/")
            else (title_el["href"] if title_el else "")
        ),
        "grade": grade,
        "model_year": year,
        "generation": vehicle.generation_for_model_year(year),
        "list_url": url,
    }

    for li in card.select(".review-detail-data__list"):
        text = _clean(li.get_text())
        if text.startswith("レビュー日"):
            row["review_date"] = text.split("：", 1)[-1].strip()
        elif text.startswith("乗車人数"):
            row["occupants"] = text.split("：", 1)[-1].strip()
        elif text.startswith("使用目的"):
            row["usage"] = text.split("：", 1)[-1].strip()

    star = card.select_one(".kr_reviewStar .star-point")
    row["score_overall"] = _num(star.get_text() if star else None)

    for item in card.select(".detail-value__item"):
        name_el = item.select_one(".detail-value__item-name")
        num_el = item.select_one(".detail-value__num")
        key = AXES.get(_clean(name_el.get_text() if name_el else ""))
        if key:
            row[f"score_{key}"] = _num(num_el.get_text() if num_el else None)

    dl = card.select_one("dl.review-data")
    if dl:
        keys = {"満足している点": "good_points", "不満な点": "bad_points", "総評": "overall_comment"}
        for dt, dd in zip(dl.select("dt"), dl.select("dd")):
            key = keys.get(_clean(dt.get_text()))
            if key:
                row[key] = _clean(dd.get_text())

    if not row["review_title"] and row["score_overall"] is None:
        return None
    return row


def collect(fetcher: Fetcher, vehicle, snapshot: str, pages: int = DEFAULT_PAGES) -> list[dict[str, Any]]:
    if not vehicle.minkara_slug:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    base_url = BASE.format(maker=vehicle.minkara_maker, slug=vehicle.minkara_slug)

    for page in range(1, pages + 1):
        url = base_url if page == 1 else f"{base_url}?pn={page}"
        try:
            soup = BeautifulSoup(fetcher.get_text(url), "lxml")
        except Exception as exc:  # noqa: BLE001 - 1車種のこけで全体を止めない
            log.warning("  minkara %s p%s: %s", vehicle.name, page, exc)
            break

        cards = soup.select("ul.review-carpage-list > li")
        if not cards:
            break
        new_on_page = 0
        for card in cards:
            parsed = _parse_card(card, vehicle, snapshot, url)
            if not parsed:
                continue
            # ページャが効かず同じページが返ってきたときに水増ししないようにする
            key = parsed["review_url"] or f'{parsed["review_title"]}|{parsed.get("review_date")}'
            if key in seen:
                continue
            seen.add(key)
            rows.append(parsed)
            new_on_page += 1
        if new_on_page == 0:
            break

    log.info("  minkara %s: %s件", vehicle.name, len(rows))
    return rows


def summarize(rows: list[dict[str, Any]], vehicle, snapshot: str) -> list[dict[str, Any]]:
    """年式ごとに評価を平均し、代表的な満足点／不満点を添える。"""
    by_year: dict[Any, list[dict[str, Any]]] = {}
    for r in rows:
        by_year.setdefault(r.get("model_year"), []).append(r)

    def mean(items: list[dict[str, Any]], key: str) -> float | None:
        values = [i[key] for i in items if isinstance(i.get(key), (int, float))]
        return round(sum(values) / len(values), 2) if values else None

    def excerpt(items: list[dict[str, Any]], key: str, limit: int = 3) -> str:
        """代表コメントを最大 limit 件つなぐ。

        別レビューでも本文が丸かぶりのことがある（同じ人が投稿し直している等）ので、
        同一文は 1 回だけにする。
        """
        picked: list[str] = []
        for item in items:
            text = (item.get(key) or "").strip()[:120]
            if text and text not in picked:
                picked.append(text)
            if len(picked) == limit:
                break
        return " / ".join(picked)

    out: list[dict[str, Any]] = []
    for year in sorted(by_year, key=lambda y: (y is None, y)):
        items = by_year[year]
        out.append(
            {
                "snapshot_date": snapshot,
                "source": "minkara",
                "vehicle_key": vehicle.key,
                "vehicle_name": vehicle.name,
                "model_year": year,
                "generation": items[0].get("generation", ""),
                "review_count": len(items),
                "score_overall": mean(items, "score_overall"),
                **{f"score_{k}": mean(items, f"score_{k}") for k in AXES.values()},
                "good_points": excerpt(items, "good_points"),
                "bad_points": excerpt(items, "bad_points"),
            }
        )
    return out
