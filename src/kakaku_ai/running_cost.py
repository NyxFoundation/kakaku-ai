"""1 年あたりの維持費を出す。

「いくらで買えるか」は相場で分かるが、**持ち続けるといくらかかるか**は
別の話で、しかもそちらのほうが総額では効く。10 年乗るなら車両価格より
維持費の累計のほうが大きいことも普通にある。

### データから出しているもの

* **値落ち** — 同じ車種の年式ごとの落札中央値の傾きから。ここがいちばん大きい費目
* **排気量** — ヤフオク商品ページのグレード表記（「2.5 S Cパッケージ」）から
  最頻値を取る。自動車税がこれで決まる
* **車両価格** — 落札中央値（ヤフオクで買う場合）

### 法定のもの（税額そのもの）

* **自動車税種別割** — 排気量で決まる。2019年10月以降の初回新規登録は減税後、
  それ以前は旧税率。**13年超のガソリン車は約15%重課**される
* **重量税** — 車両重量で決まる。2年ぶんまとめて払うので年額はその半分。
  こちらも **13年超・18年超で重課**
* **自賠責保険** — 24ヶ月 17,650円（2023年4月改定）

### 仮定（`Assumptions` で変えられる）

年間走行距離・ガソリン単価・実燃費・任意保険・車検基本料・整備費・駐車場。
**ここは人によって桁が変わる**ので、既定値は「持ち家駐車場・年8,000km・
30代の一般的な等級」くらいの想定にしてある。駐車場代は既定 0 で、
月極なら足すこと。

### 13年超で何が起きるか

古い車は値落ちが止まるので保有コストは下がるが、税が上がる。この綱引きが
どこで逆転するかを見るのがこのシートの主目的。
"""

from __future__ import annotations

import logging
import re
import statistics as st
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

log = logging.getLogger(__name__)

MANYEN = 10_000

# --------------------------------------------------------------- 法定の税額

# 自動車税種別割（自家用乗用車・年額）。2019年10月1日以降に初回新規登録した
# 車は減税後の税率、それ以前は旧税率が続く
TAX_BY_CC_NEW = ((1.0, 25_000), (1.5, 30_500), (2.0, 36_000),
                 (2.5, 43_500), (3.0, 50_000), (3.5, 57_000), (4.0, 65_500))
TAX_BY_CC_OLD = ((1.0, 29_500), (1.5, 34_500), (2.0, 39_500),
                 (2.5, 45_000), (3.0, 51_000), (3.5, 58_000), (4.0, 66_500))
TAX_REGISTRATION_CHANGE = 2019          # この年の10月に税率が下がった
TAX_OLD_CAR_SURCHARGE = 1.15            # 13年超のガソリン車は約15%重課
OLD_CAR_YEARS = 13

# 重量税（自家用乗用車・2年ぶん）。0.5t 刻みで、13年超・18年超は重課
WEIGHT_TAX_2Y = {
    1.0: (16_400, 22_800, 25_200),
    1.5: (24_600, 34_200, 37_800),
    2.0: (32_800, 45_600, 50_400),
    2.5: (41_000, 57_000, 63_000),
    3.0: (49_200, 68_400, 75_600),
}
VERY_OLD_CAR_YEARS = 18

COMPULSORY_INSURANCE_24M = 17_650       # 自賠責 24ヶ月（2023年4月〜）


@dataclass
class Assumptions:
    """人によって桁が変わるところ。既定は持ち家駐車場・年8,000km の想定。"""

    annual_km: int = 8_000
    fuel_yen_per_litre: int = 175
    voluntary_insurance: int = 60_000   # 任意保険。等級と年齢で倍は動く
    inspection_fee_2y: int = 60_000     # 車検の基本料＋整備。ディーラーならもっと
    maintenance: int = 30_000           # オイル・タイヤ・バッテリーなどの積立
    parking: int = 0                    # 月極なら 12 か月ぶんを入れる
    # 実燃費（km/L）。排気量から引く。カタログ値ではなく実走行の目安
    real_fuel_economy: dict[float, float] = field(default_factory=lambda: {
        1.5: 15.0, 2.0: 11.5, 2.5: 10.0, 3.0: 8.5, 3.5: 8.0,
    })


def _bracket(value: float, table) -> Any:
    """区分表から、値以上の最初の枠を引く。上限を超えたら最後の枠。"""
    for threshold, result in table:
        if value <= threshold + 1e-9:
            return result
    return table[-1][1]


def annual_vehicle_tax(displacement_l: float, first_registration_year: int,
                       today_year: int) -> int:
    """自動車税種別割の年額。"""
    table = (TAX_BY_CC_NEW if first_registration_year >= TAX_REGISTRATION_CHANGE
             else TAX_BY_CC_OLD)
    tax = _bracket(displacement_l, table)
    if today_year - first_registration_year >= OLD_CAR_YEARS:
        tax = round(tax * TAX_OLD_CAR_SURCHARGE / 100) * 100
    return tax


def annual_weight_tax(weight_t: float, first_registration_year: int,
                      today_year: int) -> int:
    """重量税の年額。2年ぶんの税額の半分。"""
    key = min((k for k in WEIGHT_TAX_2Y if weight_t <= k + 1e-9), default=3.0)
    age = today_year - first_registration_year
    index = 2 if age >= VERY_OLD_CAR_YEARS else (1 if age >= OLD_CAR_YEARS else 0)
    return round(WEIGHT_TAX_2Y[key][index] / 2)


def weight_class(displacement_l: float) -> float:
    """排気量から車両重量の区分を当てる。

    重量はどのサイトからも取れないので、ミニバンの実際の重量から当てている。
    1.5L 級（シエンタ・フリード）が 1.3〜1.4t、2.0L 級（セレナ・ノア・
    ステップワゴン）が 1.6〜1.8t、2.5L 以上（アルファード・エルグランド）が
    1.9〜2.2t。**これは仮定**なので、重量税は ±1万円ほどぶれうる。
    """
    if displacement_l <= 1.5:
        return 1.5
    if displacement_l <= 2.4:
        return 2.0
    return 2.5


# --------------------------------------------------------------- データ側


DISPLACEMENT = re.compile(r"(\d\.\d)")


def displacement_of(listings: list[dict[str, Any]]) -> float | None:
    """落札明細のグレード表記から排気量の最頻値を取る。

    「2.5 S Cパッケージ」のように先頭に付く。カタログを引かずに済む。
    """
    found = Counter()
    for row in listings:
        m = DISPLACEMENT.search(row.get("grade") or "")
        if m:
            value = float(m.group(1))
            if 0.6 <= value <= 5.0:
                found[value] += 1
    return found.most_common(1)[0][0] if found else None


MIN_SAMPLES_PER_YEAR = 3   # これ未満の年式は値落ちの計算に使わない


def usable_years(price_rows: list[dict[str, Any]]) -> list[tuple[int, float]]:
    """落札が十分ある年式だけを (年式, 中央値) で返す。"""
    return sorted(
        (r["model_year"], r["auction_median_manyen"])
        for r in price_rows
        if r.get("auction_median_manyen") and (r.get("auction_n") or 0) >= MIN_SAMPLES_PER_YEAR
    )


DEPRECIATION_CAP = 0.25   # 1年で車両価格の25%超は、値落ちではなく世代交代とみなす


def depreciation_at(year: int, points: list[tuple[int, float]]) -> tuple[int | None, str]:
    """その年式の車が 1 年古くなると **いくら** 下がるかを (円, 根拠) で返す。

    一定率を全車齢に当てるのはやめた。実際の値落ちは新しいうちに大きく、
    古くなると止まる。手元には年式ごとの落札中央値があるので、
    **1 つ古い年式との差額**をそのまま使うほうが素直で正しい。

    ただしこの差額は**世代交代をまたぐと壊れる**。ヴォクシーは 2013年式 36.5万 /
    2014年式 73.0万 で、2014年1月の 80系登場を挟んで倍近い差がある。これを
    1 年ぶんの値落ちとして扱うと年 27% という有り得ない数字になる。
    そこで車両価格の 25% を超える差は世代交代とみて、25% で頭打ちにする。

    戻り値の 2 つ目は根拠。`隣接` はそのまま差額、`補間` は 1 つ飛ばしの
    年式から 1 年あたりに割り戻したもの、`頭打ち` は世代交代とみて抑えたもの。
    """
    price = dict(points).get(year)
    if price is None:
        return None, ""

    older = [(y, v) for y, v in points if y < year]
    if not older:
        return None, ""
    older_year, older_price = older[-1]
    gap = year - older_year
    drop = (price - older_price) / gap
    basis = "隣接" if gap == 1 else f"補間({gap}年)"

    if drop <= 0:
        # 古いほうが高い。旧車化しているか、単にサンプルの偏り
        return 0, "値落ちなし"
    cap = price * DEPRECIATION_CAP
    if drop > cap:
        return round(cap * MANYEN), "頭打ち(世代交代)"
    return round(drop * MANYEN), basis


# --------------------------------------------------------------- 積み上げ


def estimate(
    *,
    vehicle_name: str,
    model_year: int,
    price_manyen: float,
    displacement_l: float,
    depreciation_yen: int | None,
    depreciation_basis: str,
    assumptions: Assumptions,
    today_year: int | None = None,
) -> dict[str, Any]:
    """1 台ぶんの年間維持費を積み上げる。"""
    today_year = today_year or date.today().year
    age = today_year - model_year

    depreciation = depreciation_yen

    vehicle_tax = annual_vehicle_tax(displacement_l, model_year, today_year)
    weight_tax = annual_weight_tax(weight_class(displacement_l), model_year, today_year)
    compulsory = round(COMPULSORY_INSURANCE_24M / 2)
    inspection = round(assumptions.inspection_fee_2y / 2)

    economy = _bracket(displacement_l,
                       tuple(sorted(assumptions.real_fuel_economy.items())))
    fuel = round(assumptions.annual_km / economy * assumptions.fuel_yen_per_litre)

    fixed = (vehicle_tax + weight_tax + compulsory + inspection
             + assumptions.voluntary_insurance + assumptions.maintenance
             + assumptions.parking)
    total = fixed + fuel + (depreciation or 0)

    return {
        "vehicle_name": vehicle_name,
        "model_year": model_year,
        "age": age,
        "displacement_l": displacement_l,
        "price_manyen": round(price_manyen, 1),
        "depreciation_yen": depreciation,
        "depreciation_basis": depreciation_basis,
        "depreciation_pct": (round(depreciation / (price_manyen * MANYEN) * 100, 1)
                             if depreciation and price_manyen else None),
        "vehicle_tax_yen": vehicle_tax,
        "weight_tax_yen": weight_tax,
        "compulsory_insurance_yen": compulsory,
        "voluntary_insurance_yen": assumptions.voluntary_insurance,
        "inspection_yen": inspection,
        "maintenance_yen": assumptions.maintenance,
        "parking_yen": assumptions.parking,
        "fuel_yen": fuel,
        "fuel_economy_kml": economy,
        "is_old_car": age >= OLD_CAR_YEARS,
        # 値落ちを除いた「出ていく現金」。手放すまで実感しないぶんを分けて出す
        "cash_out_yen": fixed + fuel,
        "total_yen": total,
        "total_manyen": round(total / MANYEN, 1),
        "monthly_yen": round(total / 12),
    }


def build_table(
    listings: list[dict[str, Any]],
    price_rows: list[dict[str, Any]],
    *,
    ages: tuple[int, ...] = (3, 6, 10, 14),
    assumptions: Assumptions | None = None,
    today_year: int | None = None,
) -> list[dict[str, Any]]:
    """車種 × 車齢の維持費表を組む。

    車齢をいくつか並べるのは、**古いほど税が上がり値落ちが止まる**という
    綱引きがどこで逆転するかを見るため。
    """
    assumptions = assumptions or Assumptions()
    today_year = today_year or date.today().year

    by_vehicle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in listings:
        by_vehicle[row.get("vehicle_name") or ""].append(row)
    prices_by_vehicle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in price_rows:
        prices_by_vehicle[row.get("vehicle_name") or ""].append(row)

    out: list[dict[str, Any]] = []
    for name, rows in sorted(by_vehicle.items()):
        displacement = displacement_of(rows)
        if not displacement:
            continue
        prices = prices_by_vehicle.get(name) or []
        # 価格そのものも、落札が薄い年式は使わない
        points = usable_years(prices)
        median_by_year = dict(points)
        for age in ages:
            year = today_year - age
            price = median_by_year.get(year)
            if price is None:
                continue
            drop, basis = depreciation_at(year, points)
            out.append(estimate(
                vehicle_name=name, model_year=year, price_manyen=price,
                displacement_l=displacement, depreciation_yen=drop,
                depreciation_basis=basis,
                assumptions=assumptions, today_year=today_year,
            ))
    out.sort(key=lambda r: (r["vehicle_name"], r["model_year"]))
    return out
