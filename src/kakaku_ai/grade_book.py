"""**1 車種の 1 グレードを狙って探す**ための xlsx。

`books.catalog_book()` が「どの車種にするか」を決める本なのに対して、こちらは
車種もグレードも決まっている人が **いま出ている個体を 1 台ずつ見る**ための本。
「2018〜2021年式の A8 60 TFSI を探している」のような状況で使う。

カーセンサーの在庫一覧はグレードを絞る検索軸を持っていないので、車種と年式で
引いてから**出品タイトルでグレードを判定する**。タイトルは
「A8 60 TFSI クワトロ 4WD MMIナビゲーション …」のように先頭にグレードが来る。

同じ車種の他グレードも別シートに残す。狙いのグレードが 8 台しか無いとき、
「55 TFSI なら 15 台あって 100 万安い」が見えるかどうかで判断が変わるため。
"""

from __future__ import annotations

import logging
import re
import statistics as st
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference, ScatterChart, Series
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.utils import get_column_letter

from . import classics, price_drivers, running_cost, store
from .charts import BORDER, INT_FMT, MANYEN_FMT, _header, _title
from .excel import TITLE_FONT, _write_table
from .http import Fetcher
from .sources import carsensor_listings as cl
from .vehicles import DATA_DIR
from .wide import load_catalog

log = logging.getLogger(__name__)

OUTPUT_DIR = DATA_DIR / "xlsx"


def _for_code(dataset: str, carsensor_code: str) -> list[dict[str, Any]]:
    """最新スナップショットからその車種の行だけ引く。

    車種コードの列名はデータセットで割れている。口コミとリコールは
    `vehicle_key`、相場と不具合は `carsensor_code`。片方だけ見ていたせいで
    リコールと口コミ明細のシートが**常に空**になっていたので、両方見る。
    """
    rows, _ = store.read_latest(dataset)
    return [r for r in rows
            if carsensor_code in (r.get("carsensor_code"), r.get("vehicle_key"))]


def collect_stock(
    fetcher: Fetcher,
    carsensor_code: str,
    model_name: str,
    year_from: int,
    year_to: int,
    *,
    max_pages: int = 10,
    by_drive: bool = False,
) -> list[dict[str, Any]]:
    """その車種の在庫を年式レンジで拾う。グレードの判定はあとで。

    **年式は 1 年ずつ引く。** まとめて引くとページ送りが深くなり、在庫の多い
    車種では 2 ページ目以降が 1 ページ目と同じものを返してくる（CX-8 の
    1,074台で実測。打ち切り判定が効いて 300台で止まっていた）。

    `by_drive=True` のときは 4WD と 2WD を**別々に**引いて、どちらだったかを
    `drive_type` に残す。タイトルの「4WD」表記で分けると 7% 取りこぼすため
    （`cl.DRIVE_PARAM` のコメント参照）。
    """
    vehicle = classics._Vehicle(carsensor_code, model_name)
    passes: list[tuple[str | None, dict[str, Any] | None]] = (
        [(d, {"KUDO": v}) for d, v in cl.DRIVE_PARAM.items()] if by_drive else [(None, None)]
    )

    rows: list[dict[str, Any]] = []
    for drive, params in passes:
        for year in range(year_from, year_to + 1):
            got = cl.fetch_range(fetcher, vehicle, carsensor_code, year, year,
                                 max_pages=max_pages, params=params)
            for row in got:
                row["drive_type"] = drive
            rows.extend(got)
        if drive:
            log.info("  %s %s〜%s %s: %s台", model_name, year_from, year_to, drive,
                     sum(1 for r in rows if r.get("drive_type") == drive))
    log.info("  %s %s〜%s: 計 %s台", model_name, year_from, year_to, len(rows))
    return rows


def collect_stock_by_grade(
    fetcher: Fetcher,
    carsensor_code: str,
    model_name: str,
    year_from: int,
    year_to: int,
    *,
    max_pages: int = 15,
    seats: int | None = None,
) -> list[dict[str, Any]]:
    """**公式のグレードコードで 1 グレードずつ**引く。タイトル判定より確実。

    カーセンサーの一覧には `GRDKC`（`TO_S152_F004_K075` のような絞り込みコード）
    があって、ここで引けばグレードが確定する。タイトルの先頭を読む
    `matches_grade()` と違って、販売店の書き方に左右されない。

    **乗車定員（`TEIIN`）は一覧カードに出てこない。** 定員も条件にしたいときは、
    同じグレードをもう一度その定員で引いて、両方に出てきた個体に印を付ける
    （`seats_match`）。プラド 150 の 70th は 102台のうち 7人乗りが 62台、
    5人乗りが 40台で、カードを見ただけでは区別できなかった。

    年式レンジに当たる世代のグレードだけを叩く。全世代ぶん回すと、プラドなら
    89 グレード（うち在庫のある世代は 17）になって無駄が大きい。
    """
    vehicle = classics._Vehicle(carsensor_code, model_name)
    wanted = cl.generations_for_years(fetcher, carsensor_code, year_from, year_to)
    grades = [g for g in cl.grade_codes(fetcher, carsensor_code)
              if g["generation"] in wanted]
    log.info("  %s %s〜%s年式: 世代 %s / グレード %s件",
             model_name, year_from, year_to, ",".join(sorted(wanted)), len(grades))

    rows: list[dict[str, Any]] = []
    for grade in grades:
        got = cl.fetch_range(fetcher, vehicle, carsensor_code, year_from, year_to,
                             max_pages=max_pages,
                             params={"GRDKC": grade["grade_code"]})
        if not got:
            continue
        seat_ids: set[str] = set()
        if seats:
            seat_ids = {r["listing_id"] for r in cl.fetch_range(
                fetcher, vehicle, carsensor_code, year_from, year_to,
                max_pages=max_pages,
                params={"GRDKC": grade["grade_code"], "TEIIN": seats})}
        for row in got:
            row.update({
                "grade_code": grade["grade_code"],
                "grade_name": grade["grade_name"],
                "fuel": cl.grade_fuel(grade["grade_name"]),
                "displacement_l": cl.grade_displacement(grade["grade_name"]),
                "seats_match": (row["listing_id"] in seat_ids) if seats else None,
                "seats_label": ("" if not seats else
                                f"{seats}人乗り" if row["listing_id"] in seat_ids
                                else f"{seats}人乗り以外"),
            })
        log.info("    %s: %s台%s", grade["grade_name"], len(got),
                 f"（うち{seats}人乗り {len(seat_ids)}台）" if seats else "")
        rows.extend(got)
    log.info("  %s %s〜%s年式: 計 %s台", model_name, year_from, year_to, len(rows))
    return rows


@dataclass
class Selection:
    """**狙いの条件**。グレードコード・燃料・乗車定員・走行距離で絞る。

    グレードと燃料はカーセンサーの公式グレードに紐づくので確実。乗車定員は
    `collect_stock_by_grade(seats=...)` が付けた印を見る。走行距離だけは
    検索側の選択肢が 1万km 刻みなので、ここで最終的に切る。
    """

    grade_codes: tuple[str, ...] = ()
    fuel: str | None = None
    seats: int | None = None
    mileage_max_km: int | None = None
    label: str = ""
    notes: list[str] = field(default_factory=list)

    def matches(self, row: dict[str, Any]) -> bool:
        if self.grade_codes and row.get("grade_code") not in self.grade_codes:
            return False
        if self.fuel and row.get("fuel") != self.fuel:
            return False
        if self.seats and not row.get("seats_match"):
            return False
        mileage = row.get("mileage_km")
        if self.mileage_max_km is not None:
            if mileage is None or mileage > self.mileage_max_km:
                return False
        return True

    def describe(self) -> str:
        parts: list[str] = []
        if self.grade_codes:
            parts.append(f"グレード {len(self.grade_codes)}種")
        if self.fuel:
            parts.append(self.fuel)
        if self.seats:
            parts.append(f"{self.seats}人乗り")
        if self.mileage_max_km is not None:
            parts.append(f"走行 {self.mileage_max_km / 10_000:.0f}万km以内")
        return " / ".join(parts) or "絞り込みなし"


def matches_grade(title: str, pattern: str) -> bool:
    """出品タイトルの**先頭のほう**でグレードを判定する。

    タイトルは「A8 60 TFSI クワトロ 4WD Bang&Olufsen/…」のように
    車種名 → グレード → 装備 と並ぶ。装備の羅列まで見に行くと
    「60」が別の意味（60周年記念、160馬力…）で当たるので、
    先頭 24 文字だけを見る。
    """
    return bool(re.search(pattern, (title or "")[:24]))


def build(
    *,
    output: Path,
    title: str,
    carsensor_code: str,
    model_name: str,
    grade_label: str,
    grade_pattern: str | None = None,
    drive: str | None = None,
    selection: Selection | None = None,
    year_from: int,
    year_to: int,
    stock: list[dict[str, Any]],
    displacement_l: float,
    assumptions: running_cost.Assumptions | None = None,
) -> Path:
    """狙いの個体と、その比較対象を 1 冊にまとめる。

    絞り込みの軸は 3 通り。`selection` を渡すと**カーセンサーの公式グレード
    コード・燃料・乗車定員・走行距離**で絞る（いちばん確実）。`drive` を渡すと
    駆動方式フィルタで分ける。`grade_pattern` を渡すとタイトルでグレードを
    判定して分ける（検索軸が無いので仕方なく文字列を見ている）。
    """
    assumptions = assumptions or running_cost.Assumptions()
    catalog = load_catalog().get(carsensor_code) or {}
    today_year = datetime.now().year

    if selection:
        target = [r for r in stock if selection.matches(r)]
        others = [r for r in stock if not selection.matches(r)]
        other_label = "条件外"
        other_sheet = "条件外の在庫"
    elif drive:
        target = [r for r in stock if r.get("drive_type") == drive]
        others = [r for r in stock if r.get("drive_type") != drive]
        other_label = next(d for d in cl.DRIVE_PARAM if d != drive)
        other_sheet = f"他の駆動方式_{other_label}"
    else:
        target = [r for r in stock if matches_grade(r.get("title") or "", grade_pattern or "")]
        picked = {id(r) for r in target}
        others = [r for r in stock if id(r) not in picked]
        other_label = "他グレード"
        other_sheet = "他グレード"
    for row in stock:
        row["maker"] = catalog.get("maker")
        row["model_name"] = model_name
    classics.rescore(stock)

    # 関連データ。どれも無くても落ちないようにする
    auctions, _ = store.read_latest("yahoo_used_cars")
    auctions = [r for r in auctions if (r.get("model_name") or "") == model_name]
    by_year = _for_code("wide_by_year", carsensor_code)
    defects = _for_code("catalog_defect_summary", carsensor_code)
    reviews = _for_code("catalog_review_summary", carsensor_code)
    review_details = _for_code("catalog_reviews", carsensor_code)
    recalls = _for_code("catalog_recalls", carsensor_code)

    cost_rows = _cost_rows(target, defects, displacement_l, assumptions, today_year)

    # 母集団ぜんぶ（狙い＋それ以外）で「何が値段を動かしているか」を解く。
    # 狙いだけに絞ると年式が 1〜2 年しか無くなって、年式の効果が出せない。
    seats_label = f"{selection.seats}人乗り" if selection and selection.seats else None
    drivers = price_drivers.analyse(stock, seats_label=seats_label)
    # 狙いの条件だけでも解いておく。絞り込んだあとの値段のばらつきが、
    # 年式と走行距離で説明できるのかどうかは、母集団の答えとは別物になる
    narrow = price_drivers.analyse(target)

    wb = Workbook()
    _readme(wb.active, title, grade_label, other_label, drive, year_from, year_to,
            target, others, auctions, defects, displacement_l, assumptions,
            other_sheet, catalog.get("origin"), selection, drivers)
    wb.active.title = "README"

    if drivers:
        _price_drivers_sheet(wb.create_sheet("価格要因"), drivers, narrow, grade_label)
    if target:
        _year_compare(wb.create_sheet("年式比較"), target, others,
                      grade_label, other_label, today_year)
        _price_chart(wb.create_sheet("グラフ_価格と走行"), target, others,
                     grade_label, other_label)
        _scatter(wb.create_sheet("グラフ_走行と価格"), target, grade_label)

    fit = (drivers or {}).get("fitted_by_listing") or {}
    _write_table(wb.create_sheet(f"候補_{grade_label}"), STOCK_COLUMNS,
                 sorted((_flatten(r, fit) for r in target),
                        key=lambda r: -(r["score"] or 0)),
                 number_formats=STOCK_FORMATS, wrap_columns={"why", "title"})
    if others:
        _write_table(wb.create_sheet(other_sheet), STOCK_COLUMNS,
                     sorted((_flatten(r, fit) for r in others),
                            key=lambda r: (r.get("grade_head") or "", -(r["score"] or 0))),
                     number_formats=STOCK_FORMATS, wrap_columns={"why", "title"})
    if cost_rows:
        _write_table(wb.create_sheet("年間維持費"), COST_COLUMNS, cost_rows,
                     number_formats=COST_FORMATS, wrap_columns={"repair_devices"})
    if auctions:
        _write_table(wb.create_sheet("ヤフオク落札"), AUCTION_COLUMNS,
                     sorted((_flatten_auction(r) for r in auctions),
                            key=lambda r: r["end_date"], reverse=True),
                     number_formats=AUCTION_FORMATS, wrap_columns={"title"})
    if by_year:
        _write_table(wb.create_sheet("年式別相場"), YEAR_COLUMNS,
                     sorted(by_year, key=lambda r: r["model_year"]),
                     number_formats=YEAR_FORMATS)
    if defects:
        _write_table(wb.create_sheet("壊れやすい点"), DEFECT_COLUMNS,
                     sorted(defects, key=lambda r: -(r.get("report_count") or 0)),
                     number_formats=DEFECT_FORMATS, wrap_columns={"examples"})
    if recalls:
        _write_table(wb.create_sheet("リコール"), RECALL_COLUMNS,
                     sorted(recalls, key=lambda r: r.get("notification_date") or "",
                            reverse=True),
                     number_formats={"target_units": INT_FMT},
                     wrap_columns={"situation", "measures", "models", "common_names"})
    if reviews:
        _write_table(wb.create_sheet("口コミ"), REVIEW_COLUMNS, reviews,
                     number_formats={"review_count": INT_FMT},
                     wrap_columns={"good_points", "bad_points"})
    if review_details:
        _write_table(wb.create_sheet("口コミ_明細"), REVIEW_DETAIL_COLUMNS,
                     review_details, number_formats={"model_year": "0",
                                                     "score_overall": "0.0"},
                     wrap_columns={"good_points", "bad_points", "summary"})

    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)
    log.info("%s（%s %s台 / %s %s台）",
             output.name, grade_label, len(target), other_label, len(others))
    return output


# ------------------------------------------------------------------ 列


STOCK_COLUMNS: list[tuple[str, str]] = [
    ("model_year", "年式"),
    ("drive_type", "駆動"),
    ("grade_name", "グレード（公式）"),
    ("seats_label", "乗車定員"),
    ("grade_head", "グレード（タイトル）"),
    ("score", "状態スコア"),
    ("total_price_manyen", "支払総額\n(万円)"),
    ("base_price_manyen", "車両本体\n(万円)"),
    ("fair_price_manyen", "回帰の推定\n(万円)"),
    ("gap_manyen", "推定との差\n(万円)"),
    ("mileage_mankm", "走行距離\n(万km)"),
    ("repair_history", "修復歴"),
    ("inspection", "車検"),
    ("warranty", "保証"),
    ("why", "評価の理由"),
    ("title", "装備（出品タイトル）"),
    ("url", "掲載URL"),
]
STOCK_FORMATS = {
    "model_year": "0", "score": "+0.0;-0.0;0.0",
    "total_price_manyen": MANYEN_FMT, "base_price_manyen": MANYEN_FMT,
    "fair_price_manyen": MANYEN_FMT, "gap_manyen": "+#,##0.0;-#,##0.0;0.0",
    "mileage_mankm": "0.0",
}

COST_COLUMNS: list[tuple[str, str]] = [
    ("model_year", "年式"), ("age", "車齢"),
    ("vehicle_tax_yen", "自動車税"), ("weight_tax_yen", "重量税(年割)"),
    ("compulsory_insurance_yen", "自賠責(年割)"), ("voluntary_insurance_yen", "任意保険"),
    ("inspection_yen", "車検(年割)"), ("maintenance_yen", "整備・消耗品"),
    ("annual_km", "年間走行(km)"), ("fuel_economy_kml", "実燃費(km/L)"),
    ("fuel_yen", "燃料"), ("repair_devices", "保有中に来る故障"),
    ("repair_reserve_yen", "整備予備費/年"),
    ("cash_out_yen", "現金支出/年"), ("monthly_yen", "月あたり"),
]
COST_FORMATS = {
    "model_year": "0", "age": "0", "fuel_economy_kml": "0.0",
    **{k: INT_FMT for k in ("vehicle_tax_yen", "weight_tax_yen",
                            "compulsory_insurance_yen", "voluntary_insurance_yen",
                            "inspection_yen", "maintenance_yen", "annual_km",
                            "fuel_yen", "repair_reserve_yen", "cash_out_yen",
                            "monthly_yen")},
}

AUCTION_COLUMNS: list[tuple[str, str]] = [
    ("end_date", "終了日"), ("model_year", "年式"),
    ("price_manyen", "落札額(万円)"), ("mileage_mankm", "走行(万km)"),
    ("repair_label", "修復歴"), ("bid_count", "入札数"),
    ("seller_label", "出品者"), ("title", "タイトル"), ("url", "URL"),
]
AUCTION_FORMATS = {"model_year": "0", "price_manyen": MANYEN_FMT,
                   "mileage_mankm": "0.0", "bid_count": INT_FMT}

YEAR_COLUMNS: list[tuple[str, str]] = [
    ("model_year", "年式"), ("is_open_bucket", "以前/以降まとめ"),
    ("listing_count", "掲載台数"), ("retail_p25_manyen", "25%(万円)"),
    ("retail_median_manyen", "中央値(万円)"), ("retail_p75_manyen", "75%(万円)"),
    ("url", "URL"),
]
YEAR_FORMATS = {"model_year": "0", "listing_count": INT_FMT,
                **{k: MANYEN_FMT for k in ("retail_p25_manyen",
                                           "retail_median_manyen",
                                           "retail_p75_manyen")}}

DEFECT_COLUMNS: list[tuple[str, str]] = [
    ("defective_device", "不具合装置"), ("report_count", "通報件数"),
    ("share_pct", "構成比(%)"), ("median_mileage_km", "発生時の走行距離\n中央値(km)"),
    ("model_year_min", "対象最古年式"), ("model_year_max", "対象最新年式"),
    ("examples", "実際の症状"),
]
DEFECT_FORMATS = {"report_count": INT_FMT, "share_pct": "0.0",
                  "median_mileage_km": INT_FMT,
                  "model_year_min": "0", "model_year_max": "0"}

RECALL_COLUMNS: list[tuple[str, str]] = [
    ("notification_date", "届出日"), ("notification_no", "届出番号"),
    ("defective_device", "不具合装置"), ("target_units", "対象台数"),
    ("common_names", "対象の通称名"), ("models", "対象型式"),
    ("production_from", "生産期間 から"), ("production_to", "まで"),
    ("situation", "不具合の状況"), ("measures", "改善措置"),
]

REVIEW_DETAIL_COLUMNS: list[tuple[str, str]] = [
    ("model_year", "年式"), ("grade", "グレード"), ("score_overall", "総合"),
    ("good_points", "満足している点"), ("bad_points", "不満な点"),
    ("summary", "総評"), ("url", "URL"),
]

REVIEW_COLUMNS: list[tuple[str, str]] = [
    ("review_count", "口コミ件数"), ("score_overall", "総合"),
    ("score_design", "デザイン"), ("score_driving", "走行性能"),
    ("score_ride", "乗り心地"), ("score_price", "価格"),
    ("score_fuel_economy", "燃費"),
    ("good_points", "満足している点"), ("bad_points", "不満な点"),
]


# ------------------------------------------------------------------ 中身


def _grade_head(title: str) -> str:
    """タイトルの先頭から車種名を落として、グレードらしき部分だけ返す。"""
    parts = (title or "").split()
    return " ".join(parts[1:4]) if len(parts) > 1 else ""


def _flatten(row: dict[str, Any],
             fitted: dict[str, tuple[float, float]] | None = None) -> dict[str, Any]:
    """1 行を書き出し用に均す。回帰の推定値があれば実売との差も添える。

    `推定との差` が負なら回帰の読みより安い。ただし**回帰に入っていない条件
    （色・ワンオーナー・修復の程度・地域）が悪いから安い**ことも多いので、
    安い順に並べて上から見るための取っかかりでしかない。
    """
    out = dict(row)
    out["mileage_mankm"] = round(row["mileage_km"] / 10000, 1) if row.get("mileage_km") else None
    out["why"] = " / ".join(row.get("why") or [])
    out["score"] = round(row.get("score") or 0, 1)
    out["grade_head"] = _grade_head(row.get("title") or "")
    estimate = (fitted or {}).get(row.get("listing_id") or "")
    if estimate:
        out["fair_price_manyen"] = round(estimate[0], 1)
        out["gap_manyen"] = round(estimate[1], 1)   # 実売 − 推定。負なら読みより安い
    return out


def _flatten_auction(row: dict[str, Any]) -> dict[str, Any]:
    is_store = row.get("seller_is_store")
    return {
        **row,
        "end_date": (row.get("end_time") or "")[:10],
        "price_manyen": round(row["price"] / 10000, 1) if row.get("price") else None,
        "mileage_mankm": (round(row["mileage_km"] / 10000, 1)
                          if row.get("mileage_km") else None),
        "repair_label": {"NONE": "なし", "REPAIRED": "あり",
                         "EXISTS": "あり"}.get(row.get("repair_type") or "", "不明"),
        "seller_label": "不明" if is_store is None else ("業者" if is_store else "個人"),
    }


def _cost_rows(target: list[dict[str, Any]], defects: list[dict[str, Any]],
               displacement_l: float, assumptions: running_cost.Assumptions,
               today_year: int) -> list[dict[str, Any]]:
    """狙いのグレードの年式ごとに維持費を出す。

    走行距離は在庫の実測から。落札が無い（＝この価格帯は個人売買に出てこない）
    車なので、落札側からは取れない。

    **年間走行は年式ごとではなく、対象グレード全体の中央値を使う。** 年式ごとに
    1〜2台しかないと、たまたま車庫保管の個体に当たったときに「年750km」のような
    数字になって燃料費が桁で狂う（実際 A8 2018年式が 6,000km/8年でそうなった）。
    一方、走行距離そのもの（＝いま何km）は年式ごとの中央値でよい。修理の
    見積もりはその個体が何kmかで決まるため。
    """
    by_year: dict[int, list[dict[str, Any]]] = {}
    for row in target:
        if row.get("model_year"):
            by_year.setdefault(row["model_year"], []).append(row)

    per_car = [r["mileage_km"] / max(today_year - r["model_year"], 1)
               for r in target if r.get("mileage_km") and r.get("model_year")]
    typical_km = st.median(per_car) if per_car else assumptions.annual_km

    out = []
    for year, rows in sorted(by_year.items()):
        mileages = [r["mileage_km"] for r in rows if r.get("mileage_km")]
        prices = [r["total_price_manyen"] for r in rows if r.get("total_price_manyen")]
        odometer = st.median(mileages) if mileages else 0
        annual_km = typical_km
        repair = running_cost.repair_outlook(
            defects, odometer_km=odometer, annual_km=annual_km, assumptions=assumptions
        ) if defects else None
        out.append(running_cost.estimate(
            vehicle_name="", model_year=year,
            price_manyen=st.median(prices) if prices else 0,
            displacement_l=displacement_l, depreciation_yen=None,
            depreciation_basis="", assumptions=assumptions,
            annual_km=annual_km, repair=repair, today_year=today_year,
        ))
    return out


def _wareki(year: int) -> str:
    """西暦 → 和暦の略記。中古車の広告は年式を和暦で書くことが多い。

    1月1日〜7日生まれは厳密には前の元号だが、年式は年単位でしか出回らないので
    その年のほとんどを占めるほうに寄せる（2018年＝H30、2019年＝R1）。
    """
    return f"H{year - 1988}" if year <= 2018 else f"R{year - 2018}"


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    prices = sorted(r["total_price_manyen"] for r in rows if r.get("total_price_manyen"))
    if not prices:
        return None
    km = sorted(r["mileage_km"] for r in rows if r.get("mileage_km"))
    return {
        "n": len(rows),
        "min": prices[0],
        "p25": prices[len(prices) // 4],
        "median": st.median(prices),
        "p75": prices[len(prices) * 3 // 4],
        "max": prices[-1],
        "km": st.median(km) / 10000 if km else None,
    }


def _money(value: float) -> str:
    return f"{value:+,.1f}万円"


def _factor_effect(factor: dict[str, Any], grade_base: str | None) -> str:
    """その要因が価格をどう動かすか、1 行で。

    連続量（年式・走行距離）は 1 単位あたりの金額、ダミーはその区分に入ると
    いくら動くか。グレードだけは本数が多いので、基準との差の幅で書く。
    """
    variables = factor.get("variables") or []
    if not variables:
        return ""
    if factor["factor"] == "グレード":
        values = sorted(v["coefficient"] for v in variables)
        high = max(variables, key=lambda v: v["coefficient"])
        base = f"主力の「{grade_base}」" if grade_base else "基準グレード"
        return (f"{base}に対して {values[0]:+,.0f}〜{values[-1]:+,.0f}万円。"
                f"最上位は「{high['name'].removeprefix('グレード: ')}」の"
                f"{high['coefficient']:+,.0f}万円")
    if factor["factor"] == "年式":
        return f"1年新しいと {_money(variables[0]['coefficient'])}"
    if factor["factor"] == "走行距離":
        return f"1万km 多いと {_money(variables[0]['coefficient'])}"
    if factor["factor"] == "修復歴":
        return f"修復歴ありは {_money(variables[0]['coefficient'])}"
    return f"{factor['factor']}は {_money(variables[0]['coefficient'])}"


def _verdict(factors: list[dict[str, Any]], grade_base: str | None) -> str:
    """「結局なにが効いているのか」に、金額つきで答える。

    要因の名前だけ並べても「で、いくら違うの」が分からないので、
    1 位から 3 位までは効き方の金額も添える。年式がそこに入らなかったときは、
    何番目だったかを最後に書く（たいてい聞かれるのがそこなので）。
    """
    ranked = [f for f in factors if (f["delta_r2"] or 0) > 0.005]
    if not ranked:
        return "どの要因もはっきりとは効いていない"

    def phrase(factor: dict[str, Any]) -> str:
        effect = _factor_effect(factor, grade_base)
        if not effect:
            return factor["factor"]
        # 「7人乗り（7人乗りは +24.5万円）」のような重複をたたむ
        effect = effect.removeprefix(factor["factor"]).lstrip("はの ")
        return f"{factor['factor']}（{effect}）"

    text = f"値段をいちばん動かしているのは{phrase(ranked[0])}。"
    if len(ranked) >= 2:
        text += f"次が{phrase(ranked[1])}。"
    if len(ranked) >= 3:
        text += f"その次が{phrase(ranked[2])}。"
    place = next((i for i, f in enumerate(ranked, start=1) if f["factor"] == "年式"), None)
    if place and place > 3:
        year = ranked[place - 1]
        text += f"年式は{place}番目で、{_factor_effect(year, grade_base)}しかない。"
    return text


def _narrow_note(narrow: dict[str, Any] | None, grade_label: str) -> str:
    """絞り込んだあとの値段のばらつきを、年式と走行距離で説明できるか。

    母集団（車種ぜんぶ）の答えと、狙いの条件だけの答えは別物になる。
    グレード・燃料・定員を固定して年式も走行距離も狭く切ったあとでは、
    その 2 つで説明できる余地はもう残っていないことが多い。残りは装備・色・
    車検残・販売店で、そこは表計算では決まらない。
    """
    if not narrow:
        return "台数が足りないので解いていない（30台以上必要）"
    year = next((v for v in narrow["variables"] if v["name"].startswith("年式")), None)
    mileage = next((v for v in narrow["variables"]
                    if v["name"].startswith("走行距離")), None)
    note = (f"{narrow['n']}台の価格は {narrow['price_median']:.0f}万円が中央で、"
            f"説明力は R² {narrow['r2']:.2f}。")
    if year and mileage:
        note += (f"走行1万kmで {mileage['coefficient']:+.1f}万円、"
                 f"1年新しいと {year['coefficient']:+.1f}万円 とは出るが、"
                 f"t値は {mileage['t_value']:+.1f} と {year['t_value']:+.1f} で"
                 "誤差と区別がつかない。")
    if narrow["r2"] < 0.3:
        note += ("ここまで絞ると年式も走行距離もほとんど効かない。"
                 "残っている価格差は装備・色・車検残・保証・販売店の側にある。")
    return note


def _year_vs_mileage(year: dict[str, Any], mileage: dict[str, Any],
                     trade: float | None) -> str:
    """「結局 走行距離なのか年式なのか」に 1 行で答える。

    比べるのは標準化係数。係数そのもの（万円/年 と 万円/万km）は単位が違うので
    大小を比べても意味がない。標準化係数は「その変数が実際にばらついている幅」で
    揃えてあるので、いま出回っている在庫のなかでどちらが価格差を作っているか、
    の比較になる。
    """
    year_beta = abs(year["beta"] or 0)
    mileage_beta = abs(mileage["beta"] or 0)
    if not year_beta or not mileage_beta:
        return "―"
    name = "走行距離" if mileage_beta > year_beta else "年式"
    ratio = max(year_beta, mileage_beta) / min(year_beta, mileage_beta)
    return (
        f"{name}のほうが {ratio:.1f}倍 効いている。"
        f"他を揃えると 走行1万kmで {mileage['coefficient']:+.1f}万円、"
        f"1年新しいと {year['coefficient']:+.1f}万円。"
        + (f"1年ぶんの年式差は、走行 {trade * 10:.1f}千km ぶんにしか当たらない。"
           f"1年古くても走行が {trade * 10:.1f}千km 以上少ない個体なら、そちらが得。"
           if trade and trade > 0 else "")
    )


def _price_drivers_sheet(ws, drivers: dict[str, Any],
                         narrow: dict[str, Any] | None = None,
                         grade_label: str = "") -> None:
    """値段を動かしているのはどの要因か。重回帰の結果を 1 枚に。

    上から 結論 → 要因ごとのランキング → 変数ごとの明細 の順。

    ランキングは係数の大小ではなく、その要因を外したときに説明力（R²）が
    どれだけ落ちるかで並べてある。グレードのようにダミーが何本にも割れる要因は、
    1 本あたりの係数で見ると過小評価されるため。
    """
    year_low, year_high = drivers["year_range"]
    km_low, km_high = drivers["mileage_range"]
    corr = drivers["year_mileage_correlation"]
    trade = drivers["trade_off_mankm_per_year"]
    factors = drivers.get("factors") or []
    year = next((v for v in drivers["variables"] if v["name"].startswith("年式")), None)
    mileage = next((v for v in drivers["variables"]
                    if v["name"].startswith("走行距離")), None)

    _title(ws, "値段を決めているのは何か",
           f"同じ車種の在庫 {drivers['n']}台（{year_low}〜{year_high}年式 / "
           f"走行 {km_low:.1f}〜{km_high:.1f}万km）の支払総額を、"
           "年式・走行距離・乗車定員・修復歴・グレードで説明した結果。")

    summary: list[tuple[str, Any]] = [
        ("結論", _verdict(factors, drivers.get("grade_base"))),
        ("年式 と 走行距離 ならどちらか",
         _year_vs_mileage(year, mileage, trade) if year and mileage else "―"),
        ("当てはまり",
         f"R² {drivers['r2']:.2f}、推定の誤差は ±{drivers['rmse']:.0f}万円。"
         f"残る {max(0, 1 - drivers['r2']) * 100:.0f}% は、色・ワンオーナー・装備・"
         "販売店のように一覧に出ない条件と、店ごとの値付けのクセ。"),
        ("年式と走行距離の相関",
         f"r = {corr:+.2f}。"
         + ("強く相関しているので、年式と走行距離の係数はきれいに分離できていない。"
            "どちらか一方の効果として読まないこと"
            if corr is not None and abs(corr) >= 0.8 else
            "この程度なら年式と走行距離の係数は分けて読んでよい")
         if corr is not None else "―"),
        ("この車種の中央値", f"支払総額 {drivers['price_median']:.1f}万円"),
        (f"狙い（{grade_label}）だけで見ると", _narrow_note(narrow, grade_label)),
    ]
    for i, (key, value) in enumerate(summary, start=4):
        cell = ws.cell(row=i, column=1, value=key)
        cell.font = TITLE_FONT
        cell.alignment = cell.alignment.copy(wrap_text=True, vertical="top")
        body = ws.cell(row=i, column=2, value=value)
        body.alignment = body.alignment.copy(wrap_text=True, vertical="top")
        # 下に数字の表が来るので、説明文は B:G を結合して流し込む
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=7)
        ws.row_dimensions[i].height = 46

    # 表1: 要因ごと。これが「結局なにが効いているのか」の本体
    start = 4 + len(summary) + 1
    ws.cell(row=start - 1, column=1, value="効いている順（要因ごと）").font = TITLE_FONT
    _header(ws, start, ["要因", "この要因を外すと\n説明力(R²)がどれだけ落ちるか",
                        "外したあとの\n説明力(R²)", "価格への効き方", "", "", ""])
    ws.merge_cells(start_row=start, start_column=4, end_row=start, end_column=7)
    for offset, factor in enumerate(factors):
        line = start + 1 + offset
        values = [
            factor["factor"],
            round(factor["delta_r2"], 3) if factor["delta_r2"] is not None else None,
            round(factor["r2_without"], 3) if factor["r2_without"] is not None else None,
            _factor_effect(factor, drivers.get("grade_base")),
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=line, column=col, value=value)
            cell.border = BORDER
            if col in (2, 3):
                cell.number_format = "0.000"
            if col == 4:
                cell.alignment = cell.alignment.copy(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=line, start_column=4, end_row=line, end_column=7)
    factor_last = start + len(factors)

    chart = BarChart()
    chart.type = "bar"
    chart.title = "要因ごとの説明力（外すと R² がいくら落ちるか）"
    chart.x_axis.title = "R² の落ち幅"
    chart.height, chart.width = 8, 18
    chart.add_data(Reference(ws, min_col=2, max_col=2, min_row=start, max_row=factor_last),
                   titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=start + 1, max_row=factor_last))
    ws.add_chart(chart, "I4")

    # 表2: 変数ごとの明細
    start = factor_last + 3
    ws.cell(row=start - 1, column=1, value="変数ごとの明細").font = TITLE_FONT
    head = ["変数", "係数\n(万円)", "標準誤差", "t値", "標準化係数\n(効いている順)",
            "この変数だけの\n説明力 R²", "この変数だけの\n係数(万円)"]
    _header(ws, start, head)
    for offset, variable in enumerate(drivers["variables"]):
        values = [
            variable["name"],
            round(variable["coefficient"], 2),
            round(variable["stderr"], 2) if variable["stderr"] is not None else None,
            round(variable["t_value"], 1) if variable["t_value"] is not None else None,
            round(variable["beta"], 3) if variable["beta"] is not None else None,
            round(variable["simple_r2"], 3) if variable["simple_r2"] is not None else None,
            round(variable["simple_coefficient"], 2)
            if variable["simple_coefficient"] is not None else None,
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=start + 1 + offset, column=col, value=value)
            cell.border = BORDER
            if col in (2, 3, 7):
                cell.number_format = MANYEN_FMT
            elif col == 4:
                cell.number_format = "0.0"
            elif col in (5, 6):
                cell.number_format = "0.000"

    for col, width in zip("ABCDEFG", (30, 18, 14, 10, 18, 18, 18)):
        ws.column_dimensions[col].width = width


def _scatter(ws, target: list[dict[str, Any]], grade_label: str) -> None:
    """走行距離と価格の散布図。年式ごとに系列を分ける。

    回帰の数字より先に、**点の散らばりを目で見たほうが早い**ことが多い。
    年式ごとの帯が上下にずれていれば年式が効いていて、帯のなかで右下がりなら
    走行距離が効いている。
    """
    rows = [r for r in target if r.get("mileage_km") and r.get("total_price_manyen")]
    if len(rows) < 3:
        return
    _title(ws, f"{grade_label}: 走行距離と支払総額",
           "年式ごとに系列を分けた散布図。帯が上下にずれていれば年式が、"
           "帯のなかで右下がりなら走行距離が効いている。"
           "帯から下に外れている点が、条件のわりに安い個体。")

    years = sorted({r["model_year"] for r in rows if r.get("model_year")})
    start = 4
    _header(ws, start, ["走行距離(万km)"] + [f"{y}年式(万円)" for y in years])
    for offset, row in enumerate(sorted(rows, key=lambda r: r["mileage_km"])):
        line = start + 1 + offset
        cell = ws.cell(row=line, column=1, value=round(row["mileage_km"] / 10000, 2))
        cell.border = BORDER
        cell.number_format = "0.00"
        col = years.index(row["model_year"]) + 2 if row.get("model_year") in years else None
        if col:
            value = ws.cell(row=line, column=col, value=row["total_price_manyen"])
            value.border = BORDER
            value.number_format = MANYEN_FMT
    last = start + len(rows)

    chart = ScatterChart()
    chart.style = 13
    chart.title = f"{grade_label}: 走行距離 × 支払総額（年式別）"
    chart.x_axis.title = "走行距離(万km)"
    chart.y_axis.title = "支払総額(万円)"
    chart.height, chart.width = 12, 24
    x = Reference(ws, min_col=1, min_row=start + 1, max_row=last)
    for index in range(len(years)):
        series = Series(
            Reference(ws, min_col=2 + index, min_row=start, max_row=last),
            x, title_from_data=True)
        series.marker.symbol = "circle"
        series.graphicalProperties.line.noFill = True
        chart.series.append(series)
    ws.add_chart(chart, f"{get_column_letter(len(years) + 3)}4")
    for col in range(1, len(years) + 2):
        ws.column_dimensions[get_column_letter(col)].width = 15


def _year_compare(ws, target: list[dict[str, Any]], others: list[dict[str, Any]],
                  label: str, other_label: str, today_year: int) -> None:
    """年式を 1 行にして、全年式を横並びで比べる。

    「どの年式を買うか」は値段と走行距離と残り寿命の折り合いで決まるので、
    年式ごとの価格帯・在庫の厚み・1年新しくすると何万円かかるかを並べる。

    `1年の差` は**隣り合う年式の中央値の差**であって値落ちカーブではない。
    同じ車を追いかけた数字ではなく、いま売られている別々の個体の比較なので、
    「2019年式を狙うなら2018年式より +43万円」という買い手側の差額として読む。
    """
    _title(ws, f"{label} の年式比較（全{len(target)}台）",
           f"年式ごとの支払総額と在庫の厚み。「1年の差」は隣の年式との中央値の差で、"
           f"1年新しくするのに要る上乗せ額。「{other_label}との差」が小さい年式は、"
           f"{label} を選ぶコストが安い年式。")

    by_year: dict[int, list[dict[str, Any]]] = {}
    for r in target:
        if r.get("model_year"):
            by_year.setdefault(r["model_year"], []).append(r)
    other_by_year: dict[int, list[dict[str, Any]]] = {}
    for r in others:
        if r.get("model_year"):
            other_by_year.setdefault(r["model_year"], []).append(r)

    head = ["年式", "和暦", "車齢", "台数", "最安\n(万円)", "25%\n(万円)",
            "中央値\n(万円)", "75%\n(万円)", "最高\n(万円)", "走行中央\n(万km)",
            "1年の差\n(万円)", f"{other_label}\n中央値(万円)", f"{other_label}\nとの差(万円)"]
    start = 4
    _header(ws, start, head)

    rows: list[list[Any]] = []
    prev: float | None = None
    for year in sorted(by_year):
        stat = _stats(by_year[year])
        if not stat:
            continue
        other = _stats(other_by_year.get(year) or [])
        rows.append([
            year, _wareki(year), today_year - year, stat["n"],
            round(stat["min"], 1), round(stat["p25"], 1), round(stat["median"], 1),
            round(stat["p75"], 1), round(stat["max"], 1),
            round(stat["km"], 1) if stat["km"] is not None else None,
            round(stat["median"] - prev, 1) if prev is not None else None,
            round(other["median"], 1) if other else None,
            round(stat["median"] - other["median"], 1) if other else None,
        ])
        prev = stat["median"]

    for offset, record in enumerate(rows):
        for col, value in enumerate(record, start=1):
            cell = ws.cell(row=start + 1 + offset, column=col, value=value)
            cell.border = BORDER
            if col in (1, 3):
                cell.number_format = "0"
            elif col == 4:
                cell.number_format = INT_FMT
            elif col == 10:
                cell.number_format = "0.0"
            elif col >= 5:
                cell.number_format = MANYEN_FMT
    last = start + len(rows)
    if not rows:
        return

    # 中央値の推移。狙いの区分と比較区分を重ねる
    chart = BarChart()
    chart.type = "col"
    chart.grouping = "clustered"
    chart.title = f"年式別の支払総額 中央値（{label} と {other_label}）"
    chart.y_axis.title = "万円"
    chart.height, chart.width = 10, 22
    chart.add_data(Reference(ws, min_col=7, max_col=7, min_row=start, max_row=last),
                   titles_from_data=True)
    chart.add_data(Reference(ws, min_col=12, max_col=12, min_row=start, max_row=last),
                   titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=start + 1, max_row=last))
    ws.add_chart(chart, "O4")

    # 在庫の厚みを色で。台数が少ない年式は「選べない」ので値段以前の問題になる
    ws.conditional_formatting.add(
        f"D{start + 1}:D{last}",
        ColorScaleRule(start_type="min", start_color="F8696B",
                       mid_type="percentile", mid_value=50, mid_color="FFEB84",
                       end_type="max", end_color="63BE7B"),
    )
    ws.conditional_formatting.add(
        f"G{start + 1}:G{last}",
        ColorScaleRule(start_type="min", start_color="63BE7B",
                       mid_type="percentile", mid_value=50, mid_color="FFEB84",
                       end_type="max", end_color="F8696B"),
    )
    ws.freeze_panes = ws[f"A{start + 1}"]
    for col, width in zip("ABCDEFGHIJKLM",
                          (8, 8, 7, 8, 11, 11, 12, 11, 11, 12, 12, 14, 14)):
        ws.column_dimensions[col].width = width


def _price_chart(ws, target: list[dict[str, Any]], others: list[dict[str, Any]],
                 grade_label: str, other_label: str = "他グレード") -> None:
    _title(ws, f"{grade_label} の在庫（年式別）と{other_label}との差",
           "狙いが何台あって、いくらで、どれだけ走っているか。"
           f"{other_label}との価格差が大きければ、そちらも選択肢に入る。")

    rows = []
    for label, group in (("狙い: " + grade_label, target), (other_label, others)):
        by_year: dict[int, list[dict[str, Any]]] = {}
        for r in group:
            if r.get("model_year"):
                by_year.setdefault(r["model_year"], []).append(r)
        for year, g in sorted(by_year.items()):
            prices = [r["total_price_manyen"] for r in g if r.get("total_price_manyen")]
            km = [r["mileage_km"] for r in g if r.get("mileage_km")]
            rows.append((label, year, len(g),
                         round(min(prices), 1) if prices else None,
                         round(st.median(prices), 1) if prices else None,
                         round(max(prices), 1) if prices else None,
                         round(st.median(km) / 10000, 1) if km else None))

    start = 4
    _header(ws, start, ["区分", "年式", "台数", "最安(万円)", "中央値(万円)",
                        "最高(万円)", "走行中央値(万km)"])
    for offset, record in enumerate(rows):
        for col, value in enumerate(record, start=1):
            cell = ws.cell(row=start + 1 + offset, column=col, value=value)
            cell.border = BORDER
            if col in (4, 5, 6):
                cell.number_format = MANYEN_FMT
            elif col in (3,):
                cell.number_format = INT_FMT
            elif col == 7:
                cell.number_format = "0.0"
    last = start + len(rows)
    if not rows:
        return

    chart = BarChart()
    chart.type = "bar"
    chart.grouping = "clustered"
    chart.title = "年式別の価格帯（万円）"
    chart.x_axis.title = "万円"
    chart.height, chart.width = 12, 24
    chart.add_data(Reference(ws, min_col=4, max_col=6, min_row=start, max_row=last),
                   titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=2, min_row=start + 1, max_row=last))
    ws.add_chart(chart, "I4")

    ws.conditional_formatting.add(
        f"E{start + 1}:E{last}",
        ColorScaleRule(start_type="min", start_color="63BE7B",
                       mid_type="percentile", mid_value=50, mid_color="FFEB84",
                       end_type="max", end_color="F8696B"),
    )
    ws.column_dimensions["A"].width = 20
    for col in "BCDEFG":
        ws.column_dimensions[col].width = 15


def _readme(ws, title: str, grade_label: str, other_label: str, drive: str | None,
            year_from: int, year_to: int,
            target: list[dict[str, Any]], others: list[dict[str, Any]],
            auctions: list[dict[str, Any]], defects: list[dict[str, Any]],
            displacement_l: float, assumptions: running_cost.Assumptions,
            other_sheet: str, origin: str | None = None,
            selection: Selection | None = None,
            drivers: dict[str, Any] | None = None) -> None:
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 104
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT

    prices = [r["total_price_manyen"] for r in target if r.get("total_price_manyen")]
    km = [r["mileage_km"] for r in target if r.get("mileage_km")]
    lines = [
        ("生成日時", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("対象", f"{year_from}〜{year_to}年式 / {grade_label} / 全国"),
        ("絞り込み", selection.describe() if selection else "―"),
        ("狙い",
         f"{len(target)}台"
         + (f"（{min(prices):.0f}〜{max(prices):.0f}万円 / "
            f"走行 {min(km) / 10000:.1f}〜{max(km) / 10000:.1f}万km）"
            if prices and km else "")),
        (f"比較対象（{other_label}）", f"{len(others)}台"),
        ("排気量", f"{displacement_l}L（自動車税・重量税・燃費はこれで決まる）"),
        ("", ""),
        ("■ シート", ""),
        ("価格要因",
         "何が値段を動かしているかを重回帰で解いた結果。要因ごとの順位と、"
         "年式・走行距離それぞれの効き方、「1年新しい＝走行何万km ぶん」の"
         "交換レート。"
         + (f"この車種では{drivers['factors'][0]['factor']}の差がいちばん大きく、"
            f"説明力は R² {drivers['r2']:.2f}。"
            if drivers and drivers.get("factors") else "")),
        ("年式比較", "年式を1行ずつ並べたもの。"
                 "台数・価格帯・走行距離・1年新しくするのに要る上乗せ額。"),
        (f"候補_{grade_label}", "狙いの在庫を1台ずつ。状態スコア順。"
                            "走行距離・修復歴・車検残・保証で採点している。"),
        (other_sheet,
         f"同じ車種・同じ年式レンジで、{other_label}になった在庫。"
         "狙いが薄い年式で、そちらに回る価値があるかを見るため。"
         "価格要因の回帰は、これも含めた母集団で解いている。"),
        ("グラフ_価格と走行", "年式ごとの価格帯と走行距離。比較区分との差も並べてある。"),
        ("グラフ_走行と価格", "狙いの在庫を年式ごとに色分けした散布図。"
                       "帯から下に外れている点が、条件のわりに安い個体。"),
        ("年間維持費", "税・保険・車検・燃料・整備予備費の年額。"),
        ("ヤフオク落札", "過去180日の落札実績。この価格帯の車はほとんど出てこない"
                    "ので件数は期待できない。"),
        ("年式別相場", "カーセンサー掲載の年式別価格分布（全グレード込み）。"),
        ("壊れやすい点", "国交省の不具合通報を装置別に。発生時の走行距離つき。"),
        ("リコール", "国交省リコール届出。対象の通称名の列を見て、"
                 "自分のグレードが入っているか確認する。"
                 "リコールはメーカー負担の無償修理なので費用はかからないが、"
                 "未対策のまま流通していることがあるので購入前に販売店に聞くこと。"),
        ("口コミ", "みんカラのレビュー集計と個票。"),
        ("", ""),
        ("■ 絞り込みの方法", ""),
        ("やり方",
         "カーセンサーの公式グレードコード（GRDKC）で 1 グレードずつ引いて、"
         "どのグレードだったかを各行に残している。燃料と排気量はグレード名から"
         "読む（「2.8 … ディーゼルターボ」）。乗車定員は一覧カードに出てこない"
         "ので、同じグレードを定員（TEIIN）でもう一度引いて、両方に出てきた個体に"
         "印を付けている。走行距離は検索側の選択肢が1万km刻みなので、最後に"
         "こちらで切っている。"
         if selection else
         "カーセンサーの駆動方式フィルタ（KUDO）で 4WD と 2WD を別々に引き、"
         "どちらで取れたかを各行に残している。検索側で絞っているので"
         "取りこぼしが無い。"
         if drive else
         "カーセンサーの在庫検索にグレードで絞る軸が無いので、車種と年式で引いてから"
         "出品タイトルの先頭でグレードを判定している。タイトルは"
         "「A8 60 TFSI クワトロ 4WD …」のように 車種名→グレード→装備 と並ぶ。"
         "装備の羅列まで見ると「60」が別の意味で当たるので、先頭24文字だけを見ている。"),
        ("なぜタイトルで見ないか" if drive or selection else "取りこぼし",
         "販売店の書き方に左右されないから。出品タイトルの「4WD」で数えると"
         "実数の93%しか拾えなかった（CX-8 の2018年式以降でタイトル判定 363台 / "
         "公式フィルタ 391台）。グレードも同じで、特別仕様車の表記ゆれは多い。"
         if selection else
         "出品タイトルの「4WD」で数えると実数の93%しか拾えなかった"
         "（CX-8 の2018年式以降でタイトル判定 363台 / 公式フィルタ 391台）。"
         "販売店がタイトルに書いていないだけの4WDが7%ある。"
         if drive else
         "販売店がグレードを書いていない出品は拾えない。「他グレード」シートに"
         "落ちているので、そちらも一度は目で見ること。"),
        ("", ""),
        ("■ 数字の注意", ""),
        ("維持費",
         f"税・自賠責は法定。任意保険{assumptions.voluntary_insurance:,}円・"
         f"車検基本料{assumptions.inspection_fee_2y:,}円/2年・"
         f"整備{assumptions.maintenance:,}円・{assumptions.fuel_yen_per_litre}円/L・"
         "駐車場0円を仮定。"
         + ("輸入車なので、部品代と工賃は国産より高くつく。整備費は多めに。"
            if origin == "輸入" else "")),
        ("走行距離",
         "在庫の走行距離中央値を車齢で割って年間走行としている。"
         "この価格帯は落札実績がほとんど無いので、落札側からは取れない。"),
        ("整備予備費",
         "国交省の不具合情報にある「装置ごとの発生時走行距離」を保有5年で通過するかで"
         "見積もったもの。通報件数から発生確率は出せないので、通過する装置の"
         "修理費を全部足して 0.5 を掛けている。この 0.5 は素の仮定。"
         f"この車種の通報は全部で{sum(d.get('report_count') or 0 for d in defects)}件で、"
         "母数が小さいぶん装置の並びもぶれる。"),
        ("値落ち",
         "この本には入れていない。年式ごとの落札実績が無く、"
         "掲載価格だけでは1年でいくら落ちるかを出せないため。"
         "「価格要因」の年式の係数は「いま売られている別々の個体の価格差」であって、"
         "同じ車を1年持ったときの値落ちではない。"),
        ("回帰の推定との差",
         "在庫シートの「推定との差」は 実売 − 回帰の推定。負なら読みより安い。"
         "ただし色・ワンオーナー・修復の程度・地域・販売店は回帰に入っていない。"
         "安いのには回帰が見ていない理由があることが多いので、"
         "安い順に並べて当たりを付けるまでの道具として使う。"),
    ]
    for i, (key, value) in enumerate(lines, start=3):
        cell = ws.cell(row=i, column=1, value=key)
        if key.startswith("■"):
            cell.font = TITLE_FONT
        body = ws.cell(row=i, column=2, value=value)
        body.alignment = body.alignment.copy(wrap_text=True, vertical="top")
