"""出品中のオークションを相場と突き合わせて、気になるものを拾い上げる。

やること:

1. **相場との乖離** — 積み上げた落札実績から「年式と走行距離を揃えたときの想定落札額」を
   出し、いまの価格がそこからどれだけ離れているかを見る。
2. **出品者の別** — ヤフオクが `seller.isStore` を返すので個人／ストアはそのまま分かる。
3. **リスクのフラグ** — 国交省の不具合データと出品の属性から、事実として言えることだけを並べる。

### 「壊れやすさ n%」を出さない理由

画像や説明文から故障確率を当てにいくことはできる気がするが、**答え合わせができない**。
その車が実際に壊れたかどうかのデータが手に入らないので、出した % を検証する手段がない。
検証できない数字を確度のあるふりで出すのは害のほうが大きいので、代わりに

* この世代でいちばん通報が多い装置と、その構成比（国交省 7,000件超の実績）
* その装置の**故障発生時の走行距離の中央値**と、この個体がそれを超えているか
* メーター改ざん・修復歴・13年超といった、Yahoo と年式から確定で言えること

という「根拠つきのフラグ」を出す。数えられるものだけを数える。

一方で**価格の判定は後から答え合わせができる**。監視した出品は 180 日以内に
落札されるので、予測と実際の落札額を突き合わせれば精度が測れる。
`kakaku-ai watch --backtest` がそれをやる。
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Any

from . import store
from .aggregate import MANYEN, inspection_year_month, is_usable, months_until
from .vehicles import VehicleSet, load_vehicles

log = logging.getLogger(__name__)

# 「相場より安い/高い」と言い切るしきい値（想定落札額からの乖離率）
CHEAP_PCT = -25.0
PRICEY_PCT = 30.0
# (その年式の落札件数の下限, しきい値の倍率)。実績が薄い年式ほど広く取る。
CONFIDENCE_BANDS = ((15, 1.0), (8, 1.3), (0, 1.7))
# 競り上がる前の現在価格は相場と比べても意味がない。終了までこの時間を切ったものだけ見る。
# （最初これを入れずに動かしたら、¥1スタートの新規出品が「相場より99%安い」として
#   300件中220件ヒットして使い物にならなかった）
ENDING_SOON_HOURS = 24
# 自動車税が重課になる年数
HEAVY_TAX_YEARS = 13


def _median(values: list[float]) -> float:
    values = sorted(values)
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


# --------------------------------------------------------------- 相場モデル


class PriceModel:
    """車種ごとの想定落札額。**年式は実績の中央値をそのまま使い、距離だけ回帰する。**

    最初は log(価格) ~ 年式 + 走行距離 の直線を張ったが、これが盛大に外れた。
    値落ちは 30年スパンで見ると対数直線ではないので、直線を当てると
    古い年式は高すぎ・新しい年式は安すぎに出る。学習データ自身で検証した
    セレナの残差がこれ:

        2013年 -21.6% / 2015年 -10.7% / 2017年 +27.5% / 2018年 +51.3%

    出品は新しい年式に偏るので、この歪みのせいで**ほぼ全部が「相場より高い」**
    と判定されていた（終了間際のものですら中央値 +31%）。

    そこで年式の効き方は仮定せず、**その年式の落札中央値をそのまま基準**にする。
    走行距離だけ、同一年式内の偏差に対する 1 次回帰で拾う。
    実績が薄い年式は判定しない（外挿しない）。
    """

    MIN_YEAR_SAMPLES = 3  # この件数に満たない年式は判定しない

    def __init__(self, vehicle_key: str, rows: list[dict[str, Any]]) -> None:
        self.vehicle_key = vehicle_key
        self.mileage_coef = 0.0
        self._median_price: dict[int, float] = {}
        self._median_mileage: dict[int, float] = {}
        self._count: dict[int, int] = {}

        samples = [
            (r["model_year"], (r.get("mileage_km") or 0) / 10_000, r["price"] / MANYEN)
            for r in rows
            if r.get("model_year") and r.get("price") and r.get("mileage_km") and is_usable(r)
        ]
        by_year: dict[int, list[tuple[float, float]]] = {}
        for year, mileage, price in samples:
            by_year.setdefault(year, []).append((mileage, price))

        for year, vals in by_year.items():
            self._count[year] = len(vals)
            if len(vals) >= self.MIN_YEAR_SAMPLES:
                self._median_price[year] = _median([p for _, p in vals])
                self._median_mileage[year] = _median([m for m, _ in vals])

        # 「距離が伸びるほど安い」傾きは全年式まとめて 1 本だけ推定する。
        # 年式ごとに張るとサンプルが足りない。年式内偏差に対する原点通過の回帰。
        num = den = 0.0
        for year, vals in by_year.items():
            if year not in self._median_price:
                continue
            mp, mm = self._median_price[year], self._median_mileage[year]
            for mileage, price in vals:
                dx = mileage - mm
                num += dx * (math.log(price) - math.log(mp))
                den += dx * dx
        if den > 0:
            self.mileage_coef = num / den

        self.n = len(samples)
        self.judgeable_years = sorted(self._median_price)

    def expected_manyen(self, year: int | None, mileage_km: int | None) -> float | None:
        """その年式・その距離での想定落札額（万円）。実績が薄い年式は None。"""
        if not year or year not in self._median_price:
            return None
        base = self._median_price[year]
        if mileage_km is None:
            return base
        dx = mileage_km / 10_000 - self._median_mileage[year]
        return base * math.exp(self.mileage_coef * dx)

    def samples_for(self, year: int | None) -> int:
        return self._count.get(year or 0, 0)

    def basis(self, year: int | None = None) -> str:
        return f"{year}年の落札 {self.samples_for(year)}件 + 距離補正"

    def slack(self, year: int | None = None) -> float:
        """その年式の実績が薄いほどしきい値を広げる。"""
        n = self.samples_for(year)
        for floor, factor in CONFIDENCE_BANDS:
            if n >= floor:
                return factor
        return CONFIDENCE_BANDS[-1][1]


def build_models(pool: list[dict[str, Any]]) -> dict[str, PriceModel]:
    by_vehicle: dict[str, list[dict[str, Any]]] = {}
    for r in pool:
        by_vehicle.setdefault(r.get("vehicle_key", ""), []).append(r)
    return {k: PriceModel(k, v) for k, v in by_vehicle.items()}



# ------------------------------------------------------------------ リスク


def _defect_profile(defect_rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """(車種, 世代) → 装置別の不具合集計。世代の行だけを使う。"""
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in defect_rows:
        gen = r.get("generation") or ""
        if not gen or gen == "（車種全体）":
            continue
        out.setdefault((r["vehicle_key"], gen), []).append(r)
    for rows in out.values():
        rows.sort(key=lambda r: -(r.get("report_count") or 0))
    return out


def risk_flags(
    listing: dict[str, Any],
    defects: dict[tuple[str, str], list[dict[str, Any]]],
    today: date,
) -> tuple[list[str], list[str]]:
    """(強い注意, 参考情報) を返す。どちらもデータで裏が取れるものだけ。"""
    strong: list[str] = []
    notes: list[str] = []

    mt = listing.get("mileage_type")
    if mt == "TAMPERED":
        strong.append("走行距離が改ざん扱い（TAMPERED）")
    elif mt == "METER_REPLACEMENT":
        strong.append("メーター交換歴あり")
    elif mt == "UNKNOWN_MILEAGE":
        strong.append("走行距離不明")

    repair = listing.get("repair_type")
    if repair in ("EXISTS", "REPAIRED"):
        strong.append("修復歴あり")
    elif repair == "UNKNOWN":
        # 落札実績では 2,375件中 416件（17%）がこれ。「なし」ではないので黙らせない
        notes.append("修復歴『わからない』（申告なし）")
    elif repair == "NONE":
        notes.append("修復歴なし（申告）")

    # 車検の残り。#16 の条件では車検の有無で総額が10万円前後変わる
    ym = listing.get("inspection_ym") or inspection_year_month(listing.get("inspection_until"))
    left = months_until(ym, today)
    if left is not None:
        if left < 0:
            strong.append(f"車検切れ（{ym // 100}/{ym % 100:02d} 満了）")
        elif left <= 3:
            notes.append(f"車検 残り{left}ヶ月（{ym // 100}/{ym % 100:02d} 満了）")
        else:
            notes.append(f"車検 {ym // 100}/{ym % 100:02d} まで（残り{left}ヶ月）")

    count = listing.get("image_count")
    if count is not None and count <= 3:
        strong.append(f"写真が{count}枚しかない")

    for flag in listing.get("description_flags") or []:
        strong.append(f"説明文に「{flag}」")

    year = listing.get("model_year")
    if year and today.year - year >= HEAVY_TAX_YEARS:
        notes.append(f"{today.year - year}年落ち → 自動車税が重課")

    rating = listing.get("seller_rating_pct")
    if rating is not None and rating < 95:
        strong.append(f"出品者の評価 {rating}%")

    key = (listing.get("vehicle_key", ""), listing.get("generation") or "")
    profile = defects.get(key) or []
    if profile:
        top = profile[0]
        notes.append(
            f"この世代の通報1位は「{top['defective_device']}」"
            f"（{top['report_count']}件・構成比{top['share_pct']}%）"
        )
        mileage = listing.get("mileage_km")
        med = top.get("median_mileage_km")
        if mileage and med and mileage >= med:
            strong.append(
                f"「{top['defective_device']}」の故障発生の中央走行距離 "
                f"{med/10000:.1f}万km を超過（この個体 {mileage/10000:.1f}万km）"
            )
    return strong, notes


# ------------------------------------------------------------------ 抽出


def evaluate(
    listings: list[dict[str, Any]],
    models: dict[str, PriceModel],
    defects: dict[tuple[str, str], list[dict[str, Any]]],
    today: date | None = None,
) -> list[dict[str, Any]]:
    """出品を相場と突き合わせる。

    比較の相手を揃えるのが肝。相場モデルは**落札価格**で作ってあるので、

    * 終了間際の現在価格 → そのまま落札想定と比べてよい（実測で中央値 +3.3%）
    * **即決価格 → そのままでは比べられない**。実測で個人 +32.5% / ストア +32.9% と、
      落札想定より一律に高く出る。これは「今すぐ確実に買える」ことへの上乗せで、
      モデルの誤差ではない。この上乗せぶんを差し引いてから比べないと、
      即決は全部「相場より高い」になってしまう。

    そこで即決は、同じ回で観測した即決全体の上乗せ幅（中央値）を基準にして
    「即決の中で安いか」を見る。
    """
    today = today or date.today()
    out: list[dict[str, Any]] = []

    now = datetime.now(timezone.utc)
    for r in listings:
        model = models.get(r.get("vehicle_key", ""))
        expected = model.expected_manyen(r.get("model_year"), r.get("mileage_km")) if model else None

        judge, kind, hours = _judgeable_price(r, now)
        deviation = None
        if expected and judge:
            deviation = round((judge / expected - 1) * 100, 1)

        # 現在の入札額が落札相場に対してどこにいるかは、いつでも計算できる。
        # ただし終了までまだ間があるうちは**必ず競り上がる**ので、これは
        # 「いまのところ」の下限でしかない。判定の引き金には使わず、参考として出す。
        current = (r.get("current_price") or 0) / MANYEN
        current_dev = None
        if expected and current:
            current_dev = round((current / expected - 1) * 100, 1)

        strong, notes = risk_flags(r, defects, today)
        year = r.get("model_year")
        slack = model.slack(year) if model else CONFIDENCE_BANDS[-1][1]
        out.append(
            {
                **r,
                "cheap_threshold_pct": round(CHEAP_PCT * slack, 1),
                "pricey_threshold_pct": round(PRICEY_PCT * slack, 1),
                "expected_manyen": round(expected, 1) if expected else None,
                "current_manyen": round((r.get("current_price") or 0) / MANYEN, 1),
                "judge_manyen": round(judge, 1) if judge else None,
                "current_vs_hammer_pct": current_dev,
                "will_rise": bool(hours is not None and hours > ENDING_SOON_HOURS),
                "judge_kind": kind,
                "hours_left": round(hours, 1) if hours is not None else None,
                "deviation_pct": deviation,
                "price_basis": model.basis(year) if model and expected else "相場データなし",
                "risk_strong": strong,
                "risk_notes": notes,
            }
        )

    _rebase_buy_now(out)
    return out


def _rebase_buy_now(rows: list[dict[str, Any]]) -> None:
    """即決の乖離を「即決どうしの比較」に置き直す。

    落札想定に対する上乗せ幅の中央値を引く。車種ごとに 10件以上あればその車種の
    中央値、足りなければ全体の中央値を使う。基準が変わるので `benchmark` に
    どちらで見たかを残す。
    """
    # 二度掛けを防ぐ。詳細取得のあとに一部だけ評価し直すと、その少数から
    # 基準を計算し直してしまい、表示と抽出条件が食い違う（実際にやらかした）。
    if any(r.get("benchmark") for r in rows):
        return

    buy_now = [r for r in rows if (r.get("judge_kind") == "即決") and r.get("deviation_pct") is not None]
    if not buy_now:
        for r in rows:
            r.setdefault("benchmark", "落札想定")
        return

    overall = _median([r["deviation_pct"] for r in buy_now])
    per_vehicle: dict[str, float] = {}
    grouped: dict[str, list[float]] = {}
    for r in buy_now:
        grouped.setdefault(r.get("vehicle_key", ""), []).append(r["deviation_pct"])
    for key, vals in grouped.items():
        if len(vals) >= 10:
            per_vehicle[key] = _median(vals)

    for r in rows:
        if r.get("judge_kind") == "即決" and r.get("deviation_pct") is not None:
            premium = per_vehicle.get(r.get("vehicle_key", ""), overall)
            r["buy_now_premium_pct"] = round(premium, 1)
            r["deviation_vs_hammer_pct"] = r["deviation_pct"]
            r["deviation_pct"] = round(r["deviation_pct"] - premium, 1)
            r["benchmark"] = "即決どうし"
        else:
            r["benchmark"] = "落札想定"


def _judgeable_price(
    row: dict[str, Any], now: datetime
) -> tuple[float | None, str, float | None]:
    """相場と比べてよい価格を選ぶ。

    出品直後の現在価格は競り上がる前なので相場と比べても意味がない。
    比べてよいのは次のどちらか:

    * **即決価格** — その額で今すぐ買えるので、いつ見ても判断できる
    * **終了間際の現在価格** — もう大きくは動かないので落札見込み額に近い

    どちらでもないものは判定を保留する（`kind="保留"`）。
    """
    hours = None
    end = row.get("end_time")
    if end:
        try:
            hours = (datetime.fromisoformat(end) - now).total_seconds() / 3600
        except ValueError:
            hours = None

    buy_now = row.get("buy_now_price")
    if buy_now:
        return buy_now / MANYEN, "即決", hours

    current = row.get("current_price")
    if current and hours is not None and 0 <= hours <= ENDING_SOON_HOURS:
        return current / MANYEN, f"終了{hours:.0f}h前", hours

    return None, "保留（競り上がり前）", hours


# 修復歴の絞り方。出品中の一覧では repair_type が 99% 埋まっている
# （実測 901件中 NONE 819 / EXISTS 70 / 未記載 12）ので、一覧の値でほぼ判定できる。
# 未記載の分だけは商品ページを開いたあとに再判定する。
REPAIR_MODES = ("none", "any")


def repair_ok(row: dict[str, Any], mode: str, *, resolved: bool = False) -> bool:
    """修復歴のフィルタ。

    `mode="none"` は**申告が「なし」のものだけ**。「わからない」は「なし」ではないので
    通さない（落札実績では 17% がこれ）。まだ商品ページを開いていない段階で
    値が無いものは、判定を保留して残しておく。
    """
    if mode == "any":
        return True
    repair = row.get("repair_type")
    if repair is None:
        return not resolved  # 未確認のうちは残し、確認後に落とす
    return repair == "NONE"


def pick(
    evaluated: list[dict[str, Any]],
    *,
    budget_manyen: float | None = None,
    individual_only: bool = False,
    model_year_from: int | None = None,
    year_from_by_vehicle: dict[str, int] | None = None,
    repair: str = "any",
    cheap_only: bool = False,
) -> list[dict[str, Any]]:
    """通知に値するものだけ残す。

    「相場から大きく外れている」か「強い注意フラグが立っている」もの。
    どちらでもない普通の出品は流さない（毎回大量に届いても読まれない）。
    """
    picked: list[dict[str, Any]] = []
    for r in evaluated:
        if individual_only and r.get("seller_is_store"):
            continue
        # 対象年式より古いものは流さない。古い個体は相場モデルの外挿になりやすく、
        # そもそも 13年超は自動車税が重課で選択肢から外れる。
        #
        # 下限は車種ごとに違う。同じ「何年式以降なら安心か」でも、世代交代の
        # 時期と初期ロットの落ち着き方が車種で異なるため（フリードは2代目の
        # 2017年式から、ノアは80系が落ち着く2016年式から、ヴォクシーは2017年式から）。
        floor = (year_from_by_vehicle or {}).get(r.get("vehicle_key") or "") or model_year_from
        if floor and (r.get("model_year") or 0) < floor:
            continue
        if not repair_ok(r, repair):
            continue
        if budget_manyen is not None:
            total = (r.get("judge_manyen") or r.get("current_manyen") or 0) + (
                r.get("overhead_costs") or 0
            ) / MANYEN
            if total > budget_manyen:
                continue
        # 価格を判定できないものは流さない。競り上がる前の安さは安さではない。
        dev = r.get("deviation_pct")
        if dev is None:
            continue
        if dev <= r["cheap_threshold_pct"]:
            picked.append(r)
        elif not cheap_only and dev >= r["pricey_threshold_pct"]:
            picked.append(r)

    # 安い順に並べる。乖離が出せなかったものは末尾へ。
    picked.sort(key=lambda r: (r.get("deviation_pct") is None, r.get("deviation_pct") or 0.0))
    return picked


def refresh_risk(
    rows: list[dict[str, Any]],
    defects: dict[tuple[str, str], list[dict[str, Any]]],
    today: date | None = None,
) -> None:
    """リスクのフラグだけ付け直す。価格の判定には触らない。

    商品ページを開いて写真の枚数や説明文の記載が増えたあとに使う。
    ここで `evaluate()` を丸ごと掛け直すと即決の基準が計算し直されて壊れる。
    """
    today = today or date.today()
    for r in rows:
        strong, notes = risk_flags(r, defects, today)
        r["risk_strong"] = strong
        r["risk_notes"] = notes


def load_context(vehicles: VehicleSet | None = None):
    """相場モデルと不具合プロファイルを、貯めたデータから組む。"""
    vehicles = vehicles or load_vehicles()
    pool = store.pooled_auction_listings()
    snapshots = store.list_snapshots()
    defect_rows = store.read(snapshots[-1], "defect_summary") if snapshots else []
    return build_models(pool), _defect_profile(defect_rows), vehicles
