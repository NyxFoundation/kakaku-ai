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
from .aggregate import MANYEN, is_usable
from .vehicles import VehicleSet, load_vehicles

log = logging.getLogger(__name__)

# 回帰を張るのに最低限ほしい落札件数。これ未満の車種は年式中央値にフォールバックする
MIN_FIT_SAMPLES = 15
# 「相場より安い/高い」と言い切るしきい値（想定落札額からの乖離率）。
# モデルの当てはまりが悪い車種でこれを使うと、ただのノイズを「掘り出し物」として
# 流してしまう。実測でも R² は ノア 0.84 から エルグランド 0.29 まで開きがあるので、
# 精度に応じてしきい値を広げる。
CHEAP_PCT = -25.0
PRICEY_PCT = 30.0
CONFIDENCE_BANDS = ((0.6, 1.0), (0.4, 1.4), (0.0, 1.8))  # (R²の下限, しきい値の倍率)
# 競り上がる前の現在価格は相場と比べても意味がない。終了までこの時間を切ったものだけ見る。
# （最初これを入れずに動かしたら、¥1スタートの新規出品が「相場より99%安い」として
#   300件中220件ヒットして使い物にならなかった）
ENDING_SOON_HOURS = 24
# 自動車税が重課になる年数
HEAVY_TAX_YEARS = 13


# --------------------------------------------------------------- 相場モデル


class PriceModel:
    """車種ごとに log(落札価格) ~ 年式 + 走行距離 の直線を引く。

    件数が少ない車種が多いので、凝ったモデルは置かない。年式と距離という
    効き方の大きい 2 つだけを見る。決定係数と件数を一緒に返して、
    当てにならないときは当てにならないと分かるようにする。
    """

    def __init__(self, vehicle_key: str, rows: list[dict[str, Any]]) -> None:
        self.vehicle_key = vehicle_key
        self.n = 0
        self.r2 = 0.0
        self._coef: tuple[float, float, float] | None = None
        self._median_by_year: dict[int, float] = {}

        samples = [
            (r["model_year"], (r.get("mileage_km") or 0) / 10_000, r["price"] / MANYEN)
            for r in rows
            if r.get("model_year") and r.get("price") and r.get("mileage_km") and is_usable(r)
        ]
        by_year: dict[int, list[float]] = {}
        for year, _, price in samples:
            by_year.setdefault(year, []).append(price)
        self._median_by_year = {
            y: sorted(v)[len(v) // 2] for y, v in by_year.items() if v
        }

        self.n = len(samples)
        if self.n >= MIN_FIT_SAMPLES:
            self._coef, self.r2 = _fit(samples)

    def expected_manyen(self, year: int | None, mileage_km: int | None) -> float | None:
        """年式と走行距離を揃えたときの想定落札額（万円）。"""
        if not year:
            return None
        if self._coef and mileage_km is not None:
            a, b, c = self._coef
            return math.exp(a + b * year + c * (mileage_km / 10_000))
        # 回帰が張れないときは、その年式の落札中央値で代用する（距離は揃わない）
        return self._median_by_year.get(year)

    @property
    def basis(self) -> str:
        if self._coef:
            return f"回帰 n={self.n} R²={self.r2:.2f}"
        return f"年式中央値 n={self.n}（距離補正なし）"

    @property
    def slack(self) -> float:
        """当てはまりが悪いほどしきい値を広げる倍率。"""
        r2 = self.r2 if self._coef else 0.0
        for floor, factor in CONFIDENCE_BANDS:
            if r2 >= floor:
                return factor
        return CONFIDENCE_BANDS[-1][1]


def _fit(samples: list[tuple[int, float, float]]) -> tuple[tuple[float, float, float], float]:
    """log(price) = a + b*year + c*mileage を正規方程式で解く。"""
    xs = [(1.0, float(y), m) for y, m, _ in samples]
    ys = [math.log(p) for _, _, p in samples]
    k = 3
    ata = [[sum(xs[i][r] * xs[i][c] for i in range(len(xs))) for c in range(k)] for r in range(k)]
    atb = [sum(xs[i][r] * ys[i] for i in range(len(xs))) for r in range(k)]

    # ガウスの消去法（3x3 なのでこれで十分）
    m = [row[:] + [atb[r]] for r, row in enumerate(ata)]
    for col in range(k):
        pivot = max(range(col, k), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return (0.0, 0.0, 0.0), 0.0
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(k):
            if r == col:
                continue
            f = m[r][col] / m[col][col]
            for c in range(col, k + 1):
                m[r][c] -= f * m[col][c]
    coef = tuple(m[r][k] / m[r][r] for r in range(k))  # type: ignore[assignment]

    mean = sum(ys) / len(ys)
    ss_tot = sum((y - mean) ** 2 for y in ys)
    ss_res = sum(
        (ys[i] - (coef[0] + coef[1] * xs[i][1] + coef[2] * xs[i][2])) ** 2 for i in range(len(xs))
    )
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return coef, r2  # type: ignore[return-value]


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

        strong, notes = risk_flags(r, defects, today)
        slack = model.slack if model else CONFIDENCE_BANDS[-1][1]
        out.append(
            {
                **r,
                "cheap_threshold_pct": round(CHEAP_PCT * slack, 1),
                "pricey_threshold_pct": round(PRICEY_PCT * slack, 1),
                "expected_manyen": round(expected, 1) if expected else None,
                "current_manyen": round((r.get("current_price") or 0) / MANYEN, 1),
                "judge_manyen": round(judge, 1) if judge else None,
                "judge_kind": kind,
                "hours_left": round(hours, 1) if hours is not None else None,
                "deviation_pct": deviation,
                "price_basis": model.basis if model else "相場データなし",
                "risk_strong": strong,
                "risk_notes": notes,
            }
        )
    return out


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
    repair: str = "any",
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
        if model_year_from and (r.get("model_year") or 0) < model_year_from:
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
        if dev <= r["cheap_threshold_pct"] or dev >= r["pricey_threshold_pct"]:
            picked.append(r)

    # 安い順に並べる。乖離が出せなかったものは末尾へ。
    picked.sort(key=lambda r: (r.get("deviation_pct") is None, r.get("deviation_pct") or 0.0))
    return picked


def load_context(vehicles: VehicleSet | None = None):
    """相場モデルと不具合プロファイルを、貯めたデータから組む。"""
    vehicles = vehicles or load_vehicles()
    pool = store.pooled_auction_listings()
    snapshots = store.list_snapshots()
    defect_rows = store.read(snapshots[-1], "defect_summary") if snapshots else []
    return build_models(pool), _defect_profile(defect_rows), vehicles
