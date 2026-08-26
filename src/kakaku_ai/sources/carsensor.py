"""カーセンサーの相場ページから小売相場を取る。

`https://www.carsensor.net/usedcar/souba/<CODE>/` 1ページに

* 中古車価格レンジ / 掲載台数 / 取扱店舗数
* クチコミ総合評価と 6 軸スコア
* 「本体価格と年式」= 価格帯 × 年式 の度数分布表
* 「本体価格と走行距離」= 価格帯 × 走行距離 の度数分布表

が載っている。年式別の度数分布から**グループ中央値**を補間して、
年式ごとの小売相場を推定する。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from ..http import Fetcher

log = logging.getLogger(__name__)

BASE = "https://www.carsensor.net/usedcar/souba/{code}/"

# 「420万円以上」の上端は不明なので、ビン幅ぶん上を仮の上端とする。
# 台数が少ない上位ビンなので中央値への影響は小さいが、平均には効くため注記する。
OPEN_TOP_WIDTH = 20.0


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def _int(s: str) -> int | None:
    m = re.search(r"(\d[\d,]*)", s.replace(",", ""))
    return int(m.group(1)) if m else None


def _parse_mileage_bin(label: str) -> tuple[float, float] | None:
    """走行距離ビンの見出しを万km のレンジにする。

    '0.05 万 km未満' -> (0, 0.05) / '0.5 万 | 0.7 万km' -> (0.5, 0.7)
    '15 万Km 以上'   -> (15, 20)  ※上端不明なので +5万km と仮定
    """
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", label)]
    if not nums:
        return None
    if "未満" in label:
        return (0.0, nums[-1])
    if "以上" in label:
        return (nums[0], nums[0] + 5.0)
    if len(nums) >= 2:
        return (nums[0], nums[1])
    return None


def _parse_price_bin(label: str) -> tuple[float, float] | None:
    """'40 万円~' -> (40, 60) / '20 万円未満' -> (0, 20) / '420 万円以上' -> (420, 440)"""
    n = _int(label)
    if n is None:
        return None
    if "未満" in label:
        return (0.0, float(n))
    if "以上" in label:
        return (float(n), float(n) + OPEN_TOP_WIDTH)
    return (float(n), float(n) + 20.0)


def _grouped_stats(bins: list[tuple[tuple[float, float], int]]) -> dict[str, float | int | None]:
    """(価格ビン, 台数) の列から中央値・平均・四分位を推定する。

    中央値と四分位はビン内一様分布を仮定した線形補間（グループ中央値）。
    """
    bins = [(rng, c) for rng, c in bins if c > 0]
    if not bins:
        return {"n": 0, "median": None, "mean": None, "p25": None, "p75": None,
                "min_bin": None, "max_bin": None}

    bins.sort(key=lambda x: x[0][0])
    total = sum(c for _, c in bins)
    mean = sum(((lo + hi) / 2) * c for (lo, hi), c in bins) / total

    def quantile(q: float) -> float:
        target = total * q
        cumulative = 0
        for (lo, hi), c in bins:
            if cumulative + c >= target:
                within = (target - cumulative) / c
                return lo + (hi - lo) * within
            cumulative += c
        return bins[-1][0][1]

    return {
        "n": total,
        "median": round(quantile(0.5), 1),
        "mean": round(mean, 1),
        "p25": round(quantile(0.25), 1),
        "p75": round(quantile(0.75), 1),
        "min_bin": bins[0][0][0],
        "max_bin": bins[-1][0][1],
    }


def _parse_cross_table(table) -> tuple[list[str], list[tuple[tuple[float, float], list[int]]]]:
    """価格帯 × 見出し のクロス表を (列見出し, [(価格ビン, 各列の台数)]) に変換。"""
    head_rows = table.select("thead tr")
    if not head_rows:
        return [], []
    # thead 1行目: <th colspan=2>中古車情報の相場</th> + 見出し列
    # thead 2行目: <th colspan=2>合計 N 台</th> + 列合計
    # tbody 各行 : <th>価格帯</th> <th>行合計</th> + 見出し列と同数の <td>
    columns = [_text(th) for th in head_rows[0].select("th")][1:]

    rows: list[tuple[tuple[float, float], list[int]]] = []
    for tr in table.select("tbody tr"):
        ths = tr.select("th")
        if not ths:
            continue
        price_bin = _parse_price_bin(_text(ths[0]))
        if price_bin is None:
            continue
        counts = [_int(_text(td)) or 0 for td in tr.select("td")]
        rows.append((price_bin, counts))

    # 列がずれていないかを、thead 2行目の列合計と突き合わせて検証する。
    # ページのレイアウトが変わったら黙ってズレるのではなく警告が出るようにしておく。
    if len(head_rows) > 1:
        totals = [_int(_text(th)) or 0 for th in head_rows[1].select("th")][1:]
        for col_index in range(min(len(totals), len(columns))):
            summed = sum(c[col_index] for _, c in rows if col_index < len(c))
            if summed != totals[col_index]:
                log.warning(
                    "  carsensor: 列 '%s' の合計が一致しません (表=%s, 集計=%s)。"
                    "テーブル構造が変わった可能性があります。",
                    columns[col_index],
                    totals[col_index],
                    summed,
                )
                break

    return columns, rows


# 車種ページのタイトルは「ステップワゴン(ホンダ)の中古車相場・新車価格情報【カーセンサー】」
TITLE_RE = re.compile(r"^(.+?)\((.+?)\)の中古車相場")
# 「ミニバンランキング ： 5 位」からボディタイプ（＝用途）を拾う
BODY_RE = re.compile(r"(\S+?)ランキング\s*[:：]\s*(\d+)\s*位")

# 国産メーカー。ここに無ければ輸入車として扱う
DOMESTIC_MAKERS = frozenset({
    "トヨタ", "日産", "ホンダ", "マツダ", "スバル", "スズキ", "ダイハツ", "三菱",
    "レクサス", "いすゞ", "日野", "三菱ふそう", "ＵＤトラックス", "UDトラックス",
    "光岡", "ミツオカ", "トヨタ自動車", "日産自動車",
})


def is_domestic(maker: str | None) -> bool:
    return bool(maker) and maker in DOMESTIC_MAKERS


def _summary_block(soup: BeautifulSoup, title: str = "") -> dict[str, Any]:
    """ページ上部の価格レンジ・評価・掲載台数などを拾う。"""
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    out: dict[str, Any] = {}

    # 車種名とメーカーはタイトルから。全車種を舐めるときのカタログの素になる。
    m = TITLE_RE.match(title.strip())
    if m:
        out["model_name"] = m.group(1).strip()
        out["maker"] = m.group(2).strip()
        out["is_domestic"] = is_domestic(out["maker"])

    m = re.search(r"中古車価格\s*(\d[\d,]*)\s*[~〜～]\s*(\d[\d,]*)\s*万円", text)
    if m:
        out["retail_price_min_manyen"] = int(m.group(1).replace(",", ""))
        out["retail_price_max_manyen"] = int(m.group(2).replace(",", ""))

    m = re.search(r"総合評価\s*([\d.]+)\s*点", text)
    if m:
        out["review_score_overall"] = float(m.group(1))
    m = re.search(r"クチコミ件数\s*(\d[\d,]*)\s*件", text)
    if m:
        out["review_count"] = int(m.group(1).replace(",", ""))

    for label, key in [
        ("デザイン", "review_design"),
        ("走行性", "review_driving"),
        ("居住性", "review_comfort"),
        ("運転しやすさ", "review_handling"),
        ("積載性", "review_loading"),
        ("維持費", "review_running_cost"),
    ]:
        m = re.search(rf"{label}\s*[:：]\s*([\d.]+)", text)
        if m:
            out[key] = float(m.group(1))

    m = BODY_RE.search(text)
    if m:
        # 「ミニバンランキング」→ ボディタイプ = ミニバン。用途フィルタに使う
        out["body_type"] = m.group(1)
        out["ranking_category"] = m.group(1)
        out["ranking_position"] = int(m.group(2))

    m = re.search(r"中古車掲載台数\s*[:：]\s*(\d[\d,]*)\s*台", text)
    if m:
        out["listing_count"] = int(m.group(1).replace(",", ""))
    m = re.search(r"取扱店舗\s*[:：]\s*(\d[\d,]*)\s*店舗", text)
    if m:
        out["shop_count"] = int(m.group(1).replace(",", ""))

    m = re.search(r"車種情報\s*(\d{4}年\d{1,2}月\s*[~〜～]\s*\d{4}年\d{1,2}月|\d{4}年\d{1,2}月[~〜～])", text)
    if m:
        out["production_period"] = m.group(1)

    return out


def _page(fetcher: Fetcher, code: str) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    """1ページぶんのサマリと、年式 → {ラベル, 価格ビン別台数} を返す。"""
    url = BASE.format(code=code)
    html_text = fetcher.get_text(url)
    soup = BeautifulSoup(html_text, "lxml")

    title_el = soup.select_one("title")
    summary = _summary_block(soup, title_el.get_text() if title_el else "")
    summary["carsensor_code"] = code
    summary["url"] = url

    # 「中古車情報の相場」テーブルは 年式版 → 走行距離版 の順に並ぶので先頭が年式版。
    tables = [
        t
        for t in soup.select("table.defaultTable__table")
        if "中古車情報の相場" in _text(t.select_one("thead"))
    ]
    per_year: dict[int, dict[str, Any]] = {}
    if not tables:
        return summary, per_year

    # 2 枚目は「価格帯 × 走行距離」。掲載車の走行距離中央値を出しておくと、
    # ヤフオク落札車（走行が伸びがち）と条件を揃えて読むときの物差しになる。
    if len(tables) > 1:
        mileage_cols, _ = _parse_cross_table(tables[1])
        head_rows = tables[1].select("thead tr")
        if len(head_rows) > 1:
            totals = [_int(_text(th)) or 0 for th in head_rows[1].select("th")][1:]
            bins = []
            for i, label in enumerate(mileage_cols):
                rng = _parse_mileage_bin(label)
                if rng and i < len(totals) and totals[i] > 0:
                    bins.append((rng, totals[i]))
            stats = _grouped_stats(bins)
            if stats["median"] is not None:
                # 万km -> km
                summary["retail_median_mileage_km"] = int(round(stats["median"] * 10_000))
                summary["retail_mileage_sample"] = stats["n"]

    columns, rows = _parse_cross_table(tables[0])
    for col_index, col_label in enumerate(columns):
        year = _int(col_label)
        if year is None or not (1990 <= year <= 2100):
            continue
        # 見出し列と <td> は 1 対 1（行合計は <th> なので counts には入らない）
        bins = [
            (price_bin, counts[col_index])
            for price_bin, counts in rows
            if col_index < len(counts) and counts[col_index] > 0
        ]
        if not bins:
            continue
        per_year[year] = {"label": col_label, "bins": bins}
    return summary, per_year


def collect(fetcher: Fetcher, vehicle, snapshot: str) -> dict[str, Any] | None:
    """車種サマリ + 年式別の推定小売相場を返す。

    HV が別ページに分かれている車種（アルファード / ヴェルファイア / エスティマ）は
    度数分布を足し合わせてから統計量を出す。
    """
    if not vehicle.carsensor_codes:
        log.warning("  carsensor: %s はコード未設定のためスキップ", vehicle.name)
        return None

    merged_bins: dict[int, dict[str, Any]] = {}
    summaries: list[dict[str, Any]] = []

    for code in vehicle.carsensor_codes:
        summary, per_year = _page(fetcher, code)
        summaries.append(summary)
        for year, payload in per_year.items():
            slot = merged_bins.setdefault(year, {"label": payload["label"], "bins": {}})
            for price_bin, count in payload["bins"]:
                slot["bins"][price_bin] = slot["bins"].get(price_bin, 0) + count

    # サマリは先頭ページを土台に、台数系は全ページ合算する
    base = dict(summaries[0])
    for key in ("listing_count", "shop_count", "review_count"):
        values = [s.get(key) for s in summaries if s.get(key)]
        if values:
            base[key] = sum(values)
    for key in ("retail_price_min_manyen",):
        values = [s.get(key) for s in summaries if s.get(key) is not None]
        if values:
            base[key] = min(values)
    for key in ("retail_price_max_manyen",):
        values = [s.get(key) for s in summaries if s.get(key) is not None]
        if values:
            base[key] = max(values)
    base.update(
        {
            "snapshot_date": snapshot,
            "source": "carsensor",
            "vehicle_key": vehicle.key,
            "vehicle_name": vehicle.name,
            "carsensor_code": ", ".join(vehicle.carsensor_codes),
            "url": " ".join(s["url"] for s in summaries),
        }
    )

    by_year: list[dict[str, Any]] = []
    for year in sorted(merged_bins):
        payload = merged_bins[year]
        stats = _grouped_stats(list(payload["bins"].items()))
        if not stats["n"]:
            continue
        label = payload["label"]
        by_year.append(
            {
                "snapshot_date": snapshot,
                "source": "carsensor",
                "vehicle_key": vehicle.key,
                "vehicle_name": vehicle.name,
                "model_year": year,
                "year_label": label,
                "is_open_bucket": ("以前" in label or "以降" in label),
                "generation": vehicle.generation_for_model_year(year),
                "listing_count": stats["n"],
                "retail_median_manyen": stats["median"],
                "retail_mean_manyen": stats["mean"],
                "retail_p25_manyen": stats["p25"],
                "retail_p75_manyen": stats["p75"],
                "url": summaries[0]["url"],
            }
        )

    log.info(
        "  carsensor %s: 年式 %s件 / 掲載 %s台",
        vehicle.name,
        len(by_year),
        base.get("listing_count"),
    )
    return {"summary": base, "by_year": by_year}
