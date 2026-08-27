"""全車種ぶんの 口コミ・不具合・リコール を集める。

深掘り 20 車種（`pipeline`）は世代・型式まで手で書いた config があるので
世代別に割れる。こちらはカタログの 2,237 車種が相手なので、そこまでは持てない。
代わりに **車種単位** で

* みんカラの口コミ（総合評価・6 軸・満足点・不満点）
* 国交省の不具合通報（装置別の件数と、発生時の走行距離の中央値）
* 国交省のリコール届出

を取る。世代で割れないぶん粗いが、「この車種は何がどのくらいで壊れているか」
は言える。整備の見積もり根拠にも、客への説明材料にもなる。

車種の呼び名の解決は `links` が受け持つ。ここは集めて畳むだけ。

### 取りに行く順番

全車種を毎回さらうと何時間もかかるので、**掲載台数の多い順**に処理する。
台数が多い＝いま買える玉が多い＝見られる可能性が高い車種から埋まる。
途中で止めても上位が揃っているので使える。
"""

from __future__ import annotations

import logging
import statistics as st
from typing import Any, Iterable

from . import links as links_mod
from .http import Fetcher
from .sources import minkara, mlit

log = logging.getLogger(__name__)

REVIEW_PAGES = 2  # 1ページ 5〜10件。車種単位の平均を出すにはこれで足りる


class _Vehicle:
    """`minkara` / `mlit` のコレクタに渡すための最小限の車種。

    深掘り側の `Vehicle` は世代と型式を持つが、カタログ側にはそれが無い。
    世代を聞かれたら常に「（車種全体）」を返して、1 つの塊として扱う。
    """

    ALL = "（車種全体）"

    def __init__(self, code: str, name: str, link: dict[str, Any]) -> None:
        self.key = code
        self.name = name
        minkara_link = link.get("minkara") or {}
        mlit_link = link.get("mlit") or {}
        self.minkara_maker = minkara_link.get("maker_slug")
        self.minkara_slug = minkara_link.get("slug")
        self.mlit_maker = mlit_link.get("maker")
        self.mlit_common_names = ((mlit_link.get("common_name"),)
                                  if mlit_link.get("common_name") else ())
        self.generations = ()
        # カタログ側は型式を持たないので空。リコールは通称名だけで当てる
        self.all_models: tuple[str, ...] = ()

    def generation_for_model_year(self, _year: int | None) -> str:
        return self.ALL

    def generation_label(self, _ym: int | None) -> str:
        return self.ALL

    def generation_for(self, _ym: int | None):
        return None


def _targets(summaries: list[dict[str, Any]], links: dict[str, dict[str, Any]],
             limit: int | None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows = sorted(summaries, key=lambda r: -(r.get("listing_count") or 0))
    out = [(s, links.get(s["carsensor_code"]) or {}) for s in rows]
    return out[:limit] if limit else out


def collect_reviews(fetcher: Fetcher, summaries: list[dict[str, Any]],
                    snapshot: str, *, limit: int | None = None,
                    pages: int = REVIEW_PAGES) -> tuple[list, list]:
    """みんカラの口コミを車種単位で。(個票, 車種サマリ) を返す。"""
    links = links_mod.load_links()
    details: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    targets = [(s, l) for s, l in _targets(summaries, links, limit) if (l.get("minkara"))]
    log.info("口コミ: %s車種", len(targets))
    for i, (summary, link) in enumerate(targets, 1):
        vehicle = _Vehicle(summary["carsensor_code"], summary["model_name"], link)
        try:
            rows = minkara.collect(fetcher, vehicle, snapshot, pages=pages)
        except Exception as exc:  # noqa: BLE001 - 1車種のこけで止めない
            log.warning("  %s: %s", summary["model_name"], exc)
            continue
        if not rows:
            continue
        for row in rows:
            row.update({"maker": summary.get("maker"), "body_type": summary.get("body_type"),
                        "carsensor_code": summary["carsensor_code"]})
        details.extend(rows)
        summary_rows.append(_review_summary(rows, summary, snapshot))
        if i % 100 == 0:
            log.info("  %s/%s（口コミ %s件）", i, len(targets), len(details))
    log.info("口コミ: %s車種 / %s件", len(summary_rows), len(details))
    return details, summary_rows


def _review_summary(rows: list[dict[str, Any]], summary: dict[str, Any],
                    snapshot: str) -> dict[str, Any]:
    def mean(key: str) -> float | None:
        values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        return round(sum(values) / len(values), 2) if values else None

    def excerpt(key: str, limit: int = 3) -> str:
        seen: list[str] = []
        for row in rows:
            text = (row.get(key) or "").strip()
            if text and text not in seen:
                seen.append(text)
            if len(seen) >= limit:
                break
        return "\n---\n".join(seen)

    return {
        "snapshot_date": snapshot,
        "carsensor_code": summary["carsensor_code"],
        "maker": summary.get("maker"),
        "model_name": summary.get("model_name"),
        "body_type": summary.get("body_type"),
        "review_count": len(rows),
        "score_overall": mean("score_overall"),
        "score_design": mean("score_design"),
        "score_driving": mean("score_driving"),
        "score_ride": mean("score_ride"),
        "score_loading": mean("score_loading"),
        "score_price": mean("score_price"),
        "score_fuel_economy": mean("score_fuel_economy"),
        "good_points": excerpt("good_points"),
        "bad_points": excerpt("bad_points"),
    }


def collect_defects(fetcher: Fetcher, summaries: list[dict[str, Any]],
                    snapshot: str, *, limit: int | None = None) -> tuple[list, list, list]:
    """国交省の不具合とリコール。(不具合個票, 装置別サマリ, リコール) を返す。"""
    links = links_mod.load_links()
    details: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    recall_rows: list[dict[str, Any]] = []

    targets = [(s, l) for s, l in _targets(summaries, links, limit) if l.get("mlit")]
    log.info("不具合・リコール: %s車種", len(targets))

    # リコールはメーカー単位でしか引けないので、メーカーぶんを 1 回だけ取る
    recall_cache: dict[str, list[dict[str, Any]]] = {}

    for i, (summary, link) in enumerate(targets, 1):
        vehicle = _Vehicle(summary["carsensor_code"], summary["model_name"], link)
        maker = link["mlit"]["maker"]
        try:
            rows = mlit.fetch_defects(fetcher, vehicle, snapshot, maker=maker)
        except Exception as exc:  # noqa: BLE001
            log.warning("  %s: %s", summary["model_name"], exc)
            continue
        for row in rows:
            row.update({"maker": summary.get("maker"), "body_type": summary.get("body_type"),
                        "carsensor_code": summary["carsensor_code"]})
        details.extend(rows)

        if maker not in recall_cache:
            try:
                recall_cache[maker] = mlit.fetch_recalls(fetcher, maker=maker)
            except Exception as exc:  # noqa: BLE001
                log.warning("  リコール %s: %s", maker, exc)
                recall_cache[maker] = []
        # 型式を持っていないので、通称名で当てる
        common = link["mlit"]["common_name"]
        for recall in mlit.normalize_recalls(recall_cache[maker], vehicle, snapshot):
            if common in (recall.get("common_names") or ""):
                recall_rows.append({**recall, "carsensor_code": summary["carsensor_code"],
                                    "maker": summary.get("maker"),
                                    "model_name": summary.get("model_name")})

        summary_rows.extend(_defect_summary(rows, summary, snapshot))
        if i % 50 == 0:
            log.info("  %s/%s（不具合 %s件）", i, len(targets), len(details))

    log.info("不具合 %s件 / 装置別 %s行 / リコール %s件",
             len(details), len(summary_rows), len(recall_rows))
    return details, summary_rows, recall_rows


def _defect_summary(rows: list[dict[str, Any]], summary: dict[str, Any],
                    snapshot: str) -> list[dict[str, Any]]:
    """装置別に件数を数える。世代では割らない（カタログ側に世代情報が無いため）。"""
    if not rows:
        return []
    by_device: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_device.setdefault((row.get("defective_device") or "不明").strip(), []).append(row)

    out = []
    for device, group in sorted(by_device.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        mileages = [r["mileage_km"] for r in group if r.get("mileage_km")]
        years = [r["model_year"] for r in group if r.get("model_year")]
        out.append({
            "snapshot_date": snapshot,
            "carsensor_code": summary["carsensor_code"],
            "maker": summary.get("maker"),
            "model_name": summary.get("model_name"),
            "body_type": summary.get("body_type"),
            "defective_device": device,
            "report_count": len(group),
            "share_pct": round(len(group) / len(rows) * 100, 1),
            # 「だいたい何万kmで来るか」。整備の見積もり根拠になる
            "median_mileage_km": int(st.median(mileages)) if mileages else None,
            "model_year_min": min(years) if years else None,
            "model_year_max": max(years) if years else None,
            "examples": "\n---\n".join(
                (r.get("situation") or "").strip()[:200]
                for r in sorted(group, key=lambda r: r.get("reception_date") or "",
                                reverse=True)[:3]
                if r.get("situation")
            ),
        })
    return out


def merge_into(summaries: list[dict[str, Any]],
               reviews: Iterable[dict[str, Any]],
               defects: Iterable[dict[str, Any]]) -> None:
    """車種比較の行に、口コミ評価と不具合件数を混ぜる（その場で書き換える）。"""
    review_by_code = {r["carsensor_code"]: r for r in reviews}
    defect_by_code: dict[str, list[dict[str, Any]]] = {}
    for row in defects:
        defect_by_code.setdefault(row["carsensor_code"], []).append(row)

    for summary in summaries:
        code = summary.get("carsensor_code")
        review = review_by_code.get(code)
        if review:
            summary["review_score"] = review.get("score_overall")
            summary["review_count"] = review.get("review_count")
        group = defect_by_code.get(code) or []
        if group:
            summary["defect_n"] = sum(r["report_count"] for r in group)
            top = max(group, key=lambda r: r["report_count"])
            summary["defect_top"] = f"{top['defective_device']}（{top['report_count']}件）"
            summary["defect_top_mileage_km"] = top.get("median_mileage_km")
