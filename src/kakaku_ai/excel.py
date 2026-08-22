"""全スナップショットから 1 つの xlsx を組み立てる。

方針:

* 相場は **long format（1行 = スナップショット日 × 車種 × 年式）** で持つ。
  週を重ねるほど行が増えるだけで、列は増えない。
* そのうえで「推移」シートに 行 = 車種×年式 / 列 = スナップショット日 の
  ピボットを 1 枚用意しておく。折れ線を引くならここを選ぶだけで済む。
* 最新断面だけ見たい人向けに「最新」シートも別に出す。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.chart.series import SeriesLabel
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from . import aggregate, charts, store
from .vehicles import VehicleSet, load_vehicles

log = logging.getLogger(__name__)

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=14)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
ALT_FILL = PatternFill("solid", fgColor="F2F5FA")

MANYEN_FMT = "#,##0.0"
INT_FMT = "#,##0"
PCT_FMT = "0.0"


def _write_table(
    ws: Worksheet,
    columns: Sequence[tuple[str, str]],
    rows: Iterable[dict[str, Any]],
    *,
    number_formats: dict[str, str] | None = None,
    freeze: str = "A2",
    wrap_columns: set[str] | None = None,
) -> int:
    """(キー, 見出し) の並びに従って dict の列を書き出す。"""
    number_formats = number_formats or {}
    wrap_columns = wrap_columns or set()

    for col_index, (_, header) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col_index, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER

    count = 0
    for row_index, row in enumerate(rows, start=2):
        for col_index, (key, _) in enumerate(columns, start=1):
            value = row.get(key)
            if isinstance(value, bool):
                value = "○" if value else ""
            cell = ws.cell(row=row_index, column=col_index, value=value)
            cell.border = BORDER
            if key in number_formats:
                cell.number_format = number_formats[key]
            if key in wrap_columns:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            elif row_index % 2 == 0:
                cell.fill = ALT_FILL
        count += 1

    ws.freeze_panes = freeze
    if count:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{count + 1}"

    for col_index, (key, header) in enumerate(columns, start=1):
        width = 34 if key in wrap_columns else max(10, min(24, len(header) * 2 + 4))
        ws.column_dimensions[get_column_letter(col_index)].width = width
    ws.row_dimensions[1].height = 32
    return count


# --------------------------------------------------------------------- sheets

PRICE_COLUMNS: list[tuple[str, str]] = [
    ("snapshot_date", "時点"),
    ("vehicle_name", "車種"),
    ("generation", "世代"),
    ("model_year", "年式"),
    ("auction_n", "ヤフオク\n落札件数"),
    ("auction_min_manyen", "ヤフオク\n最安(万円)"),
    ("auction_p25_manyen", "ヤフオク\n25%(万円)"),
    ("auction_median_manyen", "ヤフオク\n中央値(万円)"),
    ("auction_mean_manyen", "ヤフオク\n平均(万円)"),
    ("auction_p75_manyen", "ヤフオク\n75%(万円)"),
    ("auction_max_manyen", "ヤフオク\n最高(万円)"),
    ("auction_median_mileage_km", "落札車の\n中央走行距離(km)"),
    ("auction_unknown_repair_n", "うち修復歴\n不明の件数"),
    ("retail_n", "小売\n掲載台数"),
    ("retail_p25_manyen", "小売\n25%(万円)"),
    ("retail_median_manyen", "小売\n中央値(万円)"),
    ("retail_mean_manyen", "小売\n平均(万円)"),
    ("retail_p75_manyen", "小売\n75%(万円)"),
    ("retail_minus_auction_manyen", "小売−落札\n(万円)"),
    ("retail_premium_pct", "小売プレミアム\n(%)"),
]

PRICE_FORMATS = {
    "auction_n": INT_FMT,
    "auction_min_manyen": MANYEN_FMT,
    "auction_p25_manyen": MANYEN_FMT,
    "auction_median_manyen": MANYEN_FMT,
    "auction_mean_manyen": MANYEN_FMT,
    "auction_p75_manyen": MANYEN_FMT,
    "auction_max_manyen": MANYEN_FMT,
    "auction_median_mileage_km": INT_FMT,
    "auction_unknown_repair_n": INT_FMT,
    "retail_n": INT_FMT,
    "retail_p25_manyen": MANYEN_FMT,
    "retail_median_manyen": MANYEN_FMT,
    "retail_mean_manyen": MANYEN_FMT,
    "retail_p75_manyen": MANYEN_FMT,
    "retail_minus_auction_manyen": MANYEN_FMT,
    "retail_premium_pct": PCT_FMT,
}


def _readme_sheet(ws: Worksheet, vehicles: VehicleSet, snapshots: list[str], counts: dict[str, int]) -> None:
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 110

    ws["A1"] = f"{vehicles.maker} {vehicles.body_type} 中古車相場データベース"
    ws["A1"].font = TITLE_FONT

    lines: list[tuple[str, str]] = [
        ("生成日時", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("対象", f"{vehicles.maker}の{vehicles.body_type} {len(vehicles)}車種 / {vehicles.model_year_from}年式以降"),
        ("収録スナップショット", f"{len(snapshots)}件: {', '.join(snapshots)}"),
        ("", ""),
        ("■ シートの読み方", ""),
        ("グラフ_価格差", "落札価格と店頭価格の差が大きい順の横棒。走行距離も併記。まずここ。"),
        ("グラフ_車種別", "車種ごとの落札相場と小売相場を並べた棒グラフ。"),
        ("グラフ_年式別", "車種 × 年式 の落札中央値マトリクス（行内で色分け）と値落ちカーブ。"),
        ("相場_最新", "直近スナップショットの 車種×年式 相場。数字で見たいときはここ。"),
        ("相場_時系列", "全スナップショットを積んだ long format。ピボットの元データ。"),
        (
            "相場_累計",
            "全スナップショットの落札を auction_id で名寄せしてから年式集計したもの。"
            "ヤフオクは 180 日ぶんしか返さないが、週を重ねるとここだけ期間が伸びて "
            "n が厚くなる。サンプルが欲しいときはこちら。",
        ),
        ("推移_落札中央値", "行=車種×年式 / 列=時点。折れ線グラフはここから引く。"),
        ("推移_小売中央値", "同上、小売（カーセンサー掲載）側。"),
        ("車種サマリ", "車種単位の価格レンジ・掲載台数・満足度など。"),
        ("口コミ_年式別", "みんカラのレビューを年式で集計したもの。"),
        ("口コミ_明細", "レビュー個票（満足点・不満点・総評）。"),
        (
            "壊れやすい点",
            "国交省に寄せられた不具合通報を 世代 × 装置 で集計。整備観点の弱点はここ。"
            "世代 =「（車種全体）」の行は車種まるごとのロールアップ。",
        ),
        ("リコール", "国交省リコール届出。不具合装置・状況・改善措置つき。"),
        (
            "落札明細",
            "ヤフオク!の落札 1 台ずつ（全スナップショットを名寄せした累計）。"
            "年式・走行距離・修復歴に加え、商品ページから取ったグレード・車検・"
            "諸費用込み総額つき。",
        ),
        ("車種マスタ", "車種・世代・型式の一覧。"),
        ("", ""),
        ("■ 相場の作り方", ""),
        (
            "オークション相場",
            "ヤフオク!「中古車・新車」カテゴリの終了180日間の落札から、"
            "商品ページの firstRegYear（無ければ検索結果の carSpec.modelDate）を年式として集計。"
            "メーター交換車と修復歴ありは除外し、件数は別列に残す。"
            "修復歴『わからない』は個人出品では普通の申告なので残している（列で件数が見える）。",
        ),
        (
            "小売相場",
            "カーセンサーの相場ページにある「価格帯 × 年式」度数分布から、"
            "ビン内一様分布を仮定したグループ中央値で推定。最上位ビン（420万円以上）は幅20万円と仮定。",
        ),
        (
            "小売プレミアム",
            "(小売中央値 / 落札中央値 - 1) × 100。業販と小売の価格差の目安。",
        ),
        ("", ""),
        ("■ 出典", ""),
        ("ヤフオク!", "https://auctions.yahoo.co.jp/closedsearch/closedsearch （robots.txt で Allow）"),
        ("カーセンサー", "https://www.carsensor.net/usedcar/souba/"),
        ("価格.com", "https://kakaku.com/kuruma/"),
        ("みんカラ", "https://minkara.carview.co.jp/car/toyota/"),
        ("国土交通省", "https://renrakuda.mlit.go.jp/renrakuda/ （リコール届出情報 / 自動車不具合情報ホットライン）"),
        ("", ""),
        ("■ 注意", ""),
        (
            "サンプル数",
            "年式によっては落札件数が 1〜2 台しかない。auction_n を必ず見ること。"
            "週次で積み上がるので、時系列シート側で複数週をまとめれば精度は上がる。",
        ),
        ("価格の単位", "すべて万円（税込・諸費用別）。走行距離は km。"),
        ("更新", "毎週クロールして新しい時点の行を追記する。過去の行は書き換えない。"),
        ("リポジトリ", "https://github.com/NyxFoundation/kakaku-ai"),
        ("", ""),
        ("■ 今回のスナップショット行数", ""),
    ]
    lines += [(k, str(v)) for k, v in counts.items()]

    for i, (label, value) in enumerate(lines, start=3):
        ws.cell(row=i, column=1, value=label).font = Font(bold=label.startswith("■"))
        cell = ws.cell(row=i, column=2, value=value)
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def _pivot(
    ws: Worksheet,
    rows: list[dict[str, Any]],
    snapshots: list[str],
    value_key: str,
    value_label: str,
) -> None:
    """行 = 車種×年式 / 列 = スナップショット日 のピボット。"""
    index: dict[tuple[str, Any, str], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("vehicle_name", ""), row.get("model_year"), row.get("generation", ""))
        index.setdefault(key, {})[row["snapshot_date"]] = row.get(value_key)

    headers = ["車種", "世代", "年式"] + snapshots + ["初回比(%)"]
    for col_index, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_index, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border = BORDER

    row_index = 2
    for (name, year, generation) in sorted(
        index, key=lambda k: (k[0], -(k[1] or 0))
    ):
        series = index[(name, year, generation)]
        ws.cell(row=row_index, column=1, value=name).border = BORDER
        ws.cell(row=row_index, column=2, value=generation).border = BORDER
        ws.cell(row=row_index, column=3, value=year).border = BORDER
        values: list[float] = []
        for offset, snap in enumerate(snapshots):
            value = series.get(snap)
            cell = ws.cell(row=row_index, column=4 + offset, value=value)
            cell.number_format = MANYEN_FMT
            cell.border = BORDER
            if isinstance(value, (int, float)):
                values.append(float(value))
        if len(values) >= 2 and values[0]:
            change = ws.cell(
                row=row_index,
                column=4 + len(snapshots),
                value=round((values[-1] / values[0] - 1) * 100, 1),
            )
            change.number_format = PCT_FMT
            change.border = BORDER
        row_index += 1

    ws.freeze_panes = "D2"
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 8
    for offset in range(len(snapshots) + 1):
        ws.column_dimensions[get_column_letter(4 + offset)].width = 13

    # スナップショットが 2 つ以上たまったら折れ線を置く。
    # 1 つしかないうちは点しか描けないので出さない。
    last_row = row_index - 1
    if len(snapshots) >= 2 and last_row >= 2:
        rows_to_plot = min(last_row - 1, 20)
        chart = LineChart()
        # 取得できなかった週は空セルになる。そこで線を切らずにまたいで結ぶ。
        chart.display_blanks = "span"
        chart.title = f"{value_label}の推移（上位{rows_to_plot}行）"
        chart.y_axis.title = "万円"
        chart.x_axis.title = "時点"
        chart.height = 11
        chart.width = 24
        data = Reference(
            ws,
            min_col=4,
            max_col=3 + len(snapshots),
            min_row=1,
            max_row=1 + rows_to_plot,
        )
        chart.add_data(data, titles_from_data=False, from_rows=True)
        for series, row_offset in zip(chart.series, range(2, 2 + rows_to_plot)):
            name = f"{ws.cell(row=row_offset, column=1).value} {ws.cell(row=row_offset, column=3).value}"
            series.tx = SeriesLabel(v=name)
        ws.add_chart(chart, f"{get_column_letter(6 + len(snapshots))}2")

    log.info("  pivot %s: %s行", value_label, row_index - 2)


def build(output: Path, *, vehicles: VehicleSet | None = None) -> Path:
    vehicles = vehicles or load_vehicles()
    snapshots = store.list_snapshots()
    if not snapshots:
        raise RuntimeError("スナップショットが 1 つもありません。先に crawl を実行してください。")
    latest = snapshots[-1]

    price_all = store.read_all("price_by_year")
    price_latest = [r for r in price_all if r["snapshot_date"] == latest]
    summary_latest = store.read(latest, "vehicle_summary")
    reviews_latest = store.read(latest, "reviews")
    review_summary_latest = store.read(latest, "review_summary")
    defects_latest = store.read(latest, "defect_summary")
    recalls_latest = store.read(latest, "recalls")
    listings_latest = store.read(latest, "auction_listings")
    # 落札は「終了180日間」しか取れないので、全スナップショットを auction_id で
    # 名寄せして 1 本にする。週を重ねるほど実効期間が伸び、年式ごとの n が増える。
    listings_pool = store.pooled_auction_listings()

    wb = Workbook()
    counts = {
        "相場_時系列": len(price_all),
        "相場_最新": len(price_latest),
        "口コミ_明細": len(reviews_latest),
        "壊れやすい点": len(defects_latest),
        "リコール": len(recalls_latest),
        "落札明細_累計": len(listings_pool),
        "落札明細（最新断面のみ）": len(listings_latest),
    }

    _readme_sheet(wb.active, vehicles, snapshots, counts)
    wb.active.title = "README"

    # --- グラフ（断面）。表より先に置いて、開いてすぐ絵が見えるようにする ---
    charts.build(wb, price_latest, listings_pool, summary_latest, vehicles.model_year_from)

    # --- 車種マスタ ---
    ws = wb.create_sheet("車種マスタ")
    master_rows = []
    for v in vehicles:
        for gen in v.generations:
            master_rows.append(
                {
                    "maker": vehicles.maker,
                    "vehicle_name": v.name,
                    "vehicle_key": v.key,
                    "generation": gen.code,
                    "period": gen.label.split(" ", 1)[-1].strip("()"),
                    "models": ", ".join(gen.models),
                    "carsensor": ", ".join(v.carsensor_codes),
                    "kakaku": v.kakaku_item_id or "",
                    "minkara": v.minkara_slug or "",
                    "yahoo": ", ".join(str(c) for c in v.yahoo_categories),
                }
            )
    _write_table(
        ws,
        [
            ("maker", "メーカー"),
            ("vehicle_name", "車種"),
            ("vehicle_key", "キー"),
            ("generation", "世代"),
            ("period", "販売期間"),
            ("models", "型式"),
            ("carsensor", "カーセンサー\nコード"),
            ("kakaku", "価格.com\nID"),
            ("minkara", "みんカラ\nslug"),
            ("yahoo", "ヤフオク\nカテゴリ"),
        ],
        master_rows,
        wrap_columns={"models"},
    )

    # --- 相場 ---
    _write_table(
        wb.create_sheet("相場_最新"),
        [c for c in PRICE_COLUMNS if c[0] != "snapshot_date"],
        price_latest,
        number_formats=PRICE_FORMATS,
    )
    _write_table(
        wb.create_sheet("相場_時系列"),
        PRICE_COLUMNS,
        price_all,
        number_formats=PRICE_FORMATS,
    )

    # --- 相場_累計: 全スナップショットの落札を名寄せしてから年式集計 ---
    # 断面ごとの n が少ない年式でも、週を重ねればここは厚くなっていく。
    cumulative: list[dict[str, Any]] = []
    retail_latest = {
        (r["vehicle_key"], r["model_year"]): r for r in price_latest
    }
    by_key: dict[str, list[dict[str, Any]]] = {}
    for row in listings_pool:
        by_key.setdefault(row.get("vehicle_key", ""), []).append(row)
    for vehicle in vehicles:
        rows = by_key.get(vehicle.key)
        if not rows:
            continue
        ends = sorted(r["end_time"] for r in rows if r.get("end_time"))
        window = f"{ends[0][:10]}〜{ends[-1][:10]}" if ends else ""
        for stat in aggregate.yahoo_by_year(rows, vehicle, "累計"):
            if stat["model_year"] < vehicles.model_year_from:
                continue
            retail = retail_latest.get((vehicle.key, stat["model_year"]), {})
            retail_median = retail.get("retail_median_manyen")
            auction_median = stat["auction_median_manyen"]
            stat["retail_n"] = retail.get("retail_n")
            stat["retail_median_manyen"] = retail_median
            stat["retail_premium_pct"] = (
                round((retail_median / auction_median - 1) * 100, 1)
                if retail_median and auction_median
                else None
            )
            stat["window"] = window
            cumulative.append(stat)

    _write_table(
        wb.create_sheet("相場_累計"),
        [
            ("vehicle_name", "車種"),
            ("generation", "世代"),
            ("model_year", "年式"),
            ("auction_n", "落札件数\n(累計)"),
            ("auction_min_manyen", "最安(万円)"),
            ("auction_p25_manyen", "25%(万円)"),
            ("auction_median_manyen", "中央値(万円)"),
            ("auction_mean_manyen", "平均(万円)"),
            ("auction_p75_manyen", "75%(万円)"),
            ("auction_max_manyen", "最高(万円)"),
            ("auction_median_mileage_km", "中央走行距離(km)"),
            ("unknown_repair_n", "うち修復歴\n不明"),
            ("excluded_n", "除外件数\n(メーター交換/修復歴あり)"),
            ("retail_n", "小売\n掲載台数"),
            ("retail_median_manyen", "小売中央値\n(万円・最新)"),
            ("retail_premium_pct", "小売プレミアム\n(%)"),
            ("window", "落札の対象期間"),
        ],
        cumulative,
        number_formats={
            k: (INT_FMT if k in ("auction_n", "auction_median_mileage_km", "excluded_n", "retail_n")
                else MANYEN_FMT)
            for k in PRICE_FORMATS
        } | {"retail_premium_pct": PCT_FMT, "auction_n": INT_FMT,
             "excluded_n": INT_FMT, "retail_n": INT_FMT,
             "auction_median_mileage_km": INT_FMT},
    )
    _pivot(wb.create_sheet("推移_落札中央値"), price_all, snapshots, "auction_median_manyen", "落札中央値")
    _pivot(wb.create_sheet("推移_小売中央値"), price_all, snapshots, "retail_median_manyen", "小売中央値")

    # --- 車種サマリ ---
    _write_table(
        wb.create_sheet("車種サマリ"),
        [
            ("vehicle_name", "車種"),
            ("generations", "世代"),
            ("kakaku_new_price_min_manyen", "新車価格\n下限(万円)"),
            ("kakaku_new_price_max_manyen", "新車価格\n上限(万円)"),
            ("carsensor_retail_price_min_manyen", "中古下限\n(万円)"),
            ("carsensor_retail_price_max_manyen", "中古上限\n(万円)"),
            ("carsensor_listing_count", "掲載台数\n(カーセンサー)"),
            ("carsensor_shop_count", "取扱店舗数"),
            ("kakaku_used_listing_count", "掲載台数\n(価格.com)"),
            ("carsensor_review_score_overall", "総合評価\n(カーセンサー)"),
            ("carsensor_review_count", "口コミ件数\n(カーセンサー)"),
            ("kakaku_satisfaction_score", "満足度\n(価格.com)"),
            ("kakaku_review_count", "レビュー数\n(価格.com)"),
            ("kakaku_bbs_post_count", "クチコミ数\n(価格.com)"),
            ("carsensor_review_design", "デザイン"),
            ("carsensor_review_driving", "走行性"),
            ("carsensor_review_comfort", "居住性"),
            ("carsensor_review_handling", "運転しやすさ"),
            ("carsensor_review_loading", "積載性"),
            ("carsensor_review_running_cost", "維持費"),
            ("carsensor_ranking_position", "ミニバン\nランキング"),
        ],
        summary_latest,
        number_formats={
            "carsensor_listing_count": INT_FMT,
            "kakaku_used_listing_count": INT_FMT,
            "carsensor_shop_count": INT_FMT,
            "carsensor_review_count": INT_FMT,
            "kakaku_review_count": INT_FMT,
            "kakaku_bbs_post_count": INT_FMT,
        },
        wrap_columns={"generations"},
    )

    # --- 口コミ ---
    _write_table(
        wb.create_sheet("口コミ_年式別"),
        [
            ("vehicle_name", "車種"),
            ("generation", "世代"),
            ("model_year", "年式"),
            ("review_count", "件数"),
            ("score_overall", "おすすめ度"),
            ("score_design", "デザイン"),
            ("score_driving", "走行性能"),
            ("score_ride", "乗り心地"),
            ("score_loading", "積載性"),
            ("score_fuel_economy", "燃費"),
            ("score_price", "価格"),
            ("good_points", "満足している点"),
            ("bad_points", "不満な点"),
        ],
        review_summary_latest,
        wrap_columns={"good_points", "bad_points"},
    )
    _write_table(
        wb.create_sheet("口コミ_明細"),
        [
            ("vehicle_name", "車種"),
            ("model_year", "年式"),
            ("grade", "グレード"),
            ("review_date", "レビュー日"),
            ("usage", "使用目的"),
            ("score_overall", "おすすめ度"),
            ("score_design", "デザイン"),
            ("score_driving", "走行性能"),
            ("score_ride", "乗り心地"),
            ("score_loading", "積載性"),
            ("score_fuel_economy", "燃費"),
            ("review_title", "タイトル"),
            ("good_points", "満足している点"),
            ("bad_points", "不満な点"),
            ("overall_comment", "総評"),
            ("review_url", "URL"),
        ],
        reviews_latest,
        wrap_columns={"good_points", "bad_points", "overall_comment"},
    )

    # --- 故障・リコール ---
    _write_table(
        wb.create_sheet("壊れやすい点"),
        [
            ("vehicle_name", "車種"),
            ("generation", "世代"),
            ("defective_device", "不具合装置"),
            ("report_count", "通報件数"),
            ("share_pct", "構成比(%)"),
            ("median_mileage_km", "発生時の\n中央走行距離(km)"),
            ("model_year_min", "対象年式\n最古"),
            ("model_year_max", "対象年式\n最新"),
            ("model_codes", "型式"),
            ("examples", "代表事例（直近3件）"),
        ],
        defects_latest,
        number_formats={"report_count": INT_FMT, "median_mileage_km": INT_FMT, "share_pct": PCT_FMT},
        wrap_columns={"examples", "model_codes"},
    )
    _write_table(
        wb.create_sheet("リコール"),
        [
            ("vehicle_name", "車種"),
            ("notification_date", "届出日"),
            ("notification_no", "届出番号"),
            ("defective_device", "不具合装置"),
            ("target_units", "対象台数"),
            ("models", "型式"),
            ("production_from", "製作期間\n開始"),
            ("production_to", "製作期間\n終了"),
            ("situation", "不具合の状況"),
            ("measures", "改善措置"),
        ],
        sorted(recalls_latest, key=lambda r: (r.get("vehicle_name", ""), r.get("notification_date") or ""), reverse=False),
        number_formats={"target_units": INT_FMT},
        wrap_columns={"situation", "measures", "models"},
    )

    # --- 落札明細（累計） ---
    _write_table(
        wb.create_sheet("落札明細"),
        [
            ("vehicle_name", "車種"),
            ("model_year", "年式"),
            ("generation", "世代"),
            ("grade", "グレード"),
            ("price", "落札価格(円)"),
            ("total_price", "諸費用込み\n総額(円)"),
            ("bid_count", "入札数"),
            ("mileage_km", "走行距離(km)"),
            ("mileage_type", "距離区分"),
            ("repair_type", "修復歴"),
            ("inspection_until", "車検"),
            ("transmission", "ミッション"),
            ("fuel", "燃料"),
            ("color", "色"),
            ("end_time", "終了日時"),
            ("first_seen_snapshot", "初出\nスナップショット"),
            ("title", "商品名"),
            ("url", "URL"),
        ],
        sorted(
            listings_pool,
            key=lambda r: (r.get("vehicle_name", ""), -(r.get("model_year") or 0),
                           r.get("end_time") or ""),
        ),
        number_formats={
            "price": INT_FMT, "total_price": INT_FMT, "mileage_km": INT_FMT,
        },
        wrap_columns={"title"},
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)
    log.info("xlsx を書き出しました: %s (%s シート)", output, len(wb.sheetnames))
    return output
