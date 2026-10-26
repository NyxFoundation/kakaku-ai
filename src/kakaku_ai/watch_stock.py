"""カーセンサーの在庫を毎日見て、相場から外れているものだけ Slack に流す。

`watch`（ヤフオク出品中の監視）と同じことを**店頭在庫**でやる。

### なぜ在庫側が要るのか

**ヤフオクに出てこない車種がある。** CX-8 を実測したら、落札は過去180日で
**1台**、出品中は **0台**（カテゴリ 2084317224 は brand_id が解決すらできない）
だった。一方カーセンサーには 1,074台（うち 4WD 411台）載っている。
この手の車は、ヤフオクを見ているかぎり永久に通知が来ない。

### ヤフオク側と違うところ

* **終了時刻が無い。** 競り上がりも即決も無いので、掲載価格をそのまま相場と
  比べてよい。`watch` が苦労していた「競り上がる前の安さは安さではない」
  （¥1スタートが全部 -99% になる）問題がここには無い。
* **相場の基準が落札ではなく掲載価格。** 同じ母集団（カーセンサー在庫）の中で
  相対的に安いかを見ることになる。成約値ではないので、ここでの「相場」は
  **売り手の希望額の中央値**でしかない。実際の成約は値引き交渉ぶん下がる。
* **分散が狭い。** CX-8 4WD の 2018年式以降 363台で乖離の四分位が -6%〜+6%、
  -15%以下が19台、-25%以下は4台しかなかった。ヤフオクの落札で使っている
  -25% をそのまま当てると、ほぼ何も通知されない。だからしきい値は別に持つ。
* **出品者の評価も写真枚数も無い。** 代わりに保証・車検・修復歴が一覧に載る。

### 「安い」は安いとは限らない

小売の分散が狭いということは、そこから大きく外れた個体には**外れるだけの理由**が
あるということでもある。修復歴の記載漏れ、過走行、輸出・業販向け、現状販売。
だから -35% を超えて安いものは「割安」ではなく**注意フラグ**として立てる。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from . import classics, store, watch
from .aggregate import MANYEN, months_until
from .http import Fetcher
from .notify import STOCK_PREFIX
from .sources import carsensor_listings as cl

log = logging.getLogger(__name__)

# 掲載価格の分布はオークションより狭いので、しきい値もそのぶん狭く取る。
# 実測（CX-8 4WD 2018年式以降 363台）: 25%点 -6.0% / 中央 +0.6% / 75%点 +5.9%
CHEAP_PCT = -15.0
PRICEY_PCT = 25.0
# これを超えて安いものは「割安」ではなく「理由があるはず」として扱う
TOO_CHEAP_PCT = -35.0
# 年間走行がこれを超えると過走行として注記する
HEAVY_ANNUAL_KM = 20_000
HEAVY_TAX_YEARS = 13


# ------------------------------------------------------------------ 取得


def collect(
    fetcher: Fetcher,
    carsensor_code: str,
    model_name: str,
    *,
    year_from: int,
    year_to: int,
    drive: str | None = None,
    max_pages: int = 12,
) -> list[dict[str, Any]]:
    """監視対象の在庫を取る。

    駆動方式は**カーセンサーの検索側**（`KUDO`）で絞る。タイトルの「4WD」で
    判定すると実数の 93% しか拾えない（`cl.DRIVE_PARAM` のコメント参照）。
    """
    target = classics._Vehicle(carsensor_code, model_name)
    params = {"KUDO": cl.DRIVE_PARAM[drive]} if drive else None

    rows: list[dict[str, Any]] = []
    for year in range(year_from, year_to + 1):
        got = cl.fetch_range(fetcher, target, carsensor_code, year, year,
                             max_pages=max_pages, params=params)
        for row in got:
            row["drive_type"] = drive
            row["source"] = "carsensor_stock"
            row["vehicle_name"] = model_name
            row["wareki"] = _wareki(year)
        rows.extend(got)
    log.info("  carsensor在庫 %s %s〜%s%s: %s台", model_name, year_from, year_to,
             f" {drive}" if drive else "", len(rows))
    return rows


def _wareki(year: int) -> str:
    return f"H{year - 1988}" if year <= 2018 else f"R{year - 2018}"


# ------------------------------------------------------------------ 相場


def build_model(rows: list[dict[str, Any]]) -> watch.PriceModel:
    """在庫そのものから「年式 × 走行距離 → 支払総額」の基準を作る。

    `watch.PriceModel` をそのまま使う。あちらは落札（円）を前提にしているので、
    支払総額（万円）を円に直して渡す。年式ごとの中央値をそのまま基準にして
    距離だけ回帰する作りなので、値落ちカーブを仮定しない点がここでも効く。

    **基準は毎日その日の在庫から作り直す。** 貯めた履歴を使わないのは、
    掲載価格が成約値ではなく、売れ残りが混ざるほど上に引っ張られるため。
    「今日並んでいるものの中で安いか」だけを見る。
    """
    return watch.PriceModel("", [
        {
            "model_year": r.get("model_year"),
            "price": round((r.get("total_price_manyen") or 0) * MANYEN),
            "mileage_km": r.get("mileage_km"),
            "repair_type": "REPAIRED" if r.get("repair_history") == "あり" else "NONE",
        }
        for r in rows
    ])


def load_defects(carsensor_code: str) -> list[dict[str, Any]]:
    """国交省の不具合通報の装置別集計。無ければ空で返す（通知は止めない）。"""
    rows, _ = store.read_latest("catalog_defect_summary")
    found = [r for r in rows if r.get("carsensor_code") == carsensor_code]
    found.sort(key=lambda r: -(r.get("report_count") or 0))
    return found


# ------------------------------------------------------------------ リスク


def risk_flags(
    row: dict[str, Any],
    defects: list[dict[str, Any]],
    today: date,
    *,
    typical_km: float | None = None,
) -> tuple[list[str], list[str]]:
    """(強い注意, 参考情報)。どちらもデータで裏が取れるものだけ。

    `typical_km` は**同じ監視対象のふつうの走行距離**（中央値）。
    「故障の典型発生距離を超えている」という警告は、その典型距離がふつうの
    走行距離より下だと在庫のほぼ全部で立ってしまい、個体を見分ける役に立たない。
    そのときは強い注意ではなく参考に落とす。
    """
    strong: list[str] = []
    notes: list[str] = []

    repair = row.get("repair_history")
    if repair == "あり":
        strong.append("修復歴あり")
    elif repair == "なし":
        notes.append("修復歴なし（申告）")
    else:
        notes.append("修復歴の記載なし")

    dev = row.get("deviation_pct")
    if dev is not None and dev <= TOO_CHEAP_PCT:
        strong.append(
            f"相場より {abs(dev):.0f}% 安い。小売の分散は本来 ±6% 程度なので、"
            "ここまで外れるのは理由があるはず（現状販売・過走行・記載漏れ等）"
        )

    warranty = row.get("warranty")
    if warranty == "保証無":
        notes.append("保証なし")
    elif warranty:
        notes.append(warranty)

    ym = row.get("inspection_ym")
    if ym:
        left = months_until(ym, today)
        if left is None:
            pass
        elif left < 0:
            strong.append(f"車検切れ（{ym // 100}/{ym % 100:02d} 満了）")
        elif left <= 3:
            notes.append(f"車検 残り{left}ヶ月（{ym // 100}/{ym % 100:02d} 満了）")
        else:
            notes.append(f"車検 {ym // 100}/{ym % 100:02d} まで（残り{left}ヶ月）")
    elif row.get("inspection"):
        # 「車検整備付」「車検整備無」のように日付が無い表記はそのまま出す
        notes.append(row["inspection"])

    year = row.get("model_year")
    mileage = row.get("mileage_km")
    if year and mileage:
        annual = mileage / max(today.year - year, 1)
        if annual >= HEAVY_ANNUAL_KM:
            notes.append(f"年あたり {annual / 10000:.1f}万km の使われ方（過走行寄り）")
        if today.year - year >= HEAVY_TAX_YEARS:
            notes.append(f"{today.year - year}年落ち → 自動車税が重課")

    if defects:
        top = defects[0]
        notes.append(
            f"この車種の通報1位は「{top['defective_device']}」"
            f"（{top['report_count']}件・構成比{top.get('share_pct')}%）"
        )
        med = top.get("median_mileage_km")
        if mileage and med and mileage >= med:
            text = (f"「{top['defective_device']}」の故障発生の中央走行距離 "
                    f"{med / 10000:.1f}万km を超過（この個体 {mileage / 10000:.1f}万km）")
            # CX-8 のエンジンは通報11件で中央1.5万km、一方で在庫の走行中央は
            # 6.7万km。そのまま強い注意にすると全台で立って、通知が全部同じ顔になる。
            informative = typical_km is None or med >= typical_km
            (strong if informative else notes).append(text)
    return strong, notes


# ------------------------------------------------------------------ 判定


def evaluate(
    rows: list[dict[str, Any]],
    model: watch.PriceModel,
    defects: list[dict[str, Any]],
    today: date | None = None,
) -> list[dict[str, Any]]:
    """1台ずつ、同年式・同距離の掲載相場からどれだけ離れているかを出す。"""
    today = today or date.today()
    mileages = sorted(r["mileage_km"] for r in rows if r.get("mileage_km"))
    typical_km = mileages[len(mileages) // 2] if mileages else None

    out: list[dict[str, Any]] = []
    for r in rows:
        year = r.get("model_year")
        expected = model.expected_manyen(year, r.get("mileage_km"))
        price = r.get("total_price_manyen")
        dev = round((price / expected - 1) * 100, 1) if expected and price else None
        slack = model.slack(year)
        row = {
            **r,
            "expected_manyen": round(expected, 1) if expected else None,
            "deviation_pct": dev,
            "cheap_threshold_pct": round(CHEAP_PCT * slack, 1),
            "pricey_threshold_pct": round(PRICEY_PCT * slack, 1),
            "price_basis": (f"{year}年の掲載 {model.samples_for(year)}台 + 距離補正"
                            if expected else "同年式の掲載が薄く判定できない"),
        }
        strong, notes = risk_flags(row, defects, today, typical_km=typical_km)
        row["risk_strong"] = strong
        row["risk_notes"] = notes
        out.append(row)
    return out


def pick(
    evaluated: list[dict[str, Any]],
    *,
    budget_manyen: float | None = None,
    model_year_from: int | None = None,
    repair_free: bool = True,
    cheap_only: bool = True,
) -> list[dict[str, Any]]:
    """通知に値するものだけ残す。安い順。"""
    picked: list[dict[str, Any]] = []
    for r in evaluated:
        if model_year_from and (r.get("model_year") or 0) < model_year_from:
            continue
        # 「なし」の申告があるものだけ。記載なしは「なし」ではないので通さない
        if repair_free and r.get("repair_history") != "なし":
            continue
        if budget_manyen is not None and (r.get("total_price_manyen") or 0) > budget_manyen:
            continue
        dev = r.get("deviation_pct")
        if dev is None:
            continue
        if dev <= r["cheap_threshold_pct"]:
            picked.append(r)
        elif not cheap_only and dev >= r["pricey_threshold_pct"]:
            picked.append(r)

    picked.sort(key=lambda r: r.get("deviation_pct") or 0.0)
    return picked


# ------------------------------------------------------------------ 既読


def seen_key(row: dict[str, Any]) -> str:
    return STOCK_PREFIX + row["listing_id"]


def prune_seen(seen: dict[str, str], alive: list[dict[str, Any]]) -> dict[str, str]:
    """掲載が消えた個体を既読から落とす。

    在庫には終了日時が無いので、ヤフオク側の「終了から14日で整理」が使えない。
    代わりに**いま載っているか**で判断する。消えた ID は二度と出てこない
    （カーセンサーの在庫 ID は個体ごとに固有）ので、落として安全。

    ヤフオクの既読まで巻き込まないよう、`cs:` で始まるキーだけを対象にする。
    取得に失敗して 0 台だったときは何もしない（全部消してしまうため）。
    """
    if not alive:
        return seen
    live = {STOCK_PREFIX + r["listing_id"] for r in alive}
    kept = {k: v for k, v in seen.items()
            if not k.startswith(STOCK_PREFIX) or k in live}
    dropped = len(seen) - len(kept)
    if dropped:
        log.info("  既読リストから掲載終了 %s件を整理（残り %s件）", dropped, len(kept))
    return kept
