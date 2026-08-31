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


# 装置別の想定修理費（円）。**これは実測ではなく相場観**。国交省の不具合情報は
# 「何が壊れたか」は教えてくれるが「いくらかかったか」は入っていないので、
# 一般的な工賃＋部品代を置いている。ディーラーか町工場か、リビルト品を使うかで
# 倍は動くので、そこは各自で差し替える前提。
REPAIR_COST = {
    "エンジン": 250_000,
    "動力伝達": 300_000,      # AT・CVT・駆動系。いちばん高くつく
    "制動装置": 60_000,
    "保安灯火": 40_000,
    "車枠・車体": 80_000,
    "乗車装置": 70_000,
    "かじ取り": 90_000,
    "緩衝装置": 80_000,
    "電気装置": 60_000,
    "排ｶﾞｽ･騒音": 100_000,
    "燃料装置": 90_000,
    "冷房装置": 120_000,
    "その他": 50_000,
}
REPAIR_COST_DEFAULT = 60_000

# 通報の多い装置のうち、上位いくつまでを見るか。裾は 1〜2 件しかなく、
# 「その車種の弱点」とは言えない
TOP_DEVICES = 6
MIN_DEVICE_SHARE_PCT = 3.0


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
    # 保有期間。修理の見積もりはこの期間に何km走るかで決まる
    ownership_years: int = 5
    # 「典型発生距離を通過する装置」のうち、実際に何割が自分に起きるとみるか。
    # 通報件数から確率は出せない（母数＝その車種の総保有台数が分からないし、
    # 通報するのはトラブルに遭った人のごく一部）ので、これは**素の仮定**。
    # 0.5 は「弱点として挙がっている装置の半分は当たる」という置き方
    repair_hit_rate: float = 0.5
    repair_cost: dict[str, int] = field(default_factory=lambda: dict(REPAIR_COST))


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


# --------------------------------------------------------------- 走行距離


def annual_km_of(price_rows: list[dict[str, Any]], today_year: int) -> int | None:
    """その車種が実際に **1年あたり何km走られているか** を落札実績から出す。

    年8,000km のような一律の仮定を置くと、車種で燃料費が変わらなくなってしまう。
    落札明細には走行距離が入っているので、年式ごとの中央値を車齢で割れば
    その車種の使われ方が出る。実際ミニバンは車種で 1万〜1.5万km/年 と差がある。

    年式ごとに `走行距離 ÷ 車齢` を出して中央値を取る。落札が 3 件未満の年式は
    使わない（1台の走行距離がそのまま中央値になってしまうため）。
    """
    rates = [
        r["auction_median_mileage_km"] / (today_year - r["model_year"])
        for r in price_rows
        if r.get("auction_median_mileage_km")
        and (r.get("auction_n") or 0) >= MIN_SAMPLES_PER_YEAR
        and today_year - r["model_year"] >= 2      # 車齢1年以下は分母が小さすぎる
    ]
    return round(st.median(rates)) if len(rates) >= 3 else None


# --------------------------------------------------------------- 修理の見積もり


def repair_outlook(
    defect_rows: list[dict[str, Any]],
    *,
    odometer_km: float,
    annual_km: float,
    assumptions: Assumptions,
) -> dict[str, Any]:
    """保有期間中に **典型的な故障の距離帯を通過するか** を見て修理費を積む。

    国交省の不具合情報には装置ごとに「発生時の走行距離の中央値」が入っている。
    いま何kmで、これから何km走るかが分かれば、その距離帯を通過するかどうかは
    言える。通過するなら、その装置の修理費を見ておくべき、という積み方。

    **通報件数から発生確率は出せない。** 母数（その車種が何台走っているか）が
    分からないうえ、不具合に遭っても通報する人はごく一部だから。なので
    「通過する装置ぜんぶの修理費」を上限として出し、そこに `repair_hit_rate`
    （既定 0.5）を掛けたものを予備費としている。この 0.5 は素の仮定。

    リコールはここに入れない。**リコールはメーカー負担で無償修理**なので
    持ち主の出費にはならない。別途「未対策なら確認すべき」として件数だけ出す。
    """
    end_km = odometer_km + annual_km * assumptions.ownership_years
    devices = sorted(
        (d for d in defect_rows if (d.get("share_pct") or 0) >= MIN_DEVICE_SHARE_PCT),
        key=lambda d: -(d.get("report_count") or 0),
    )[:TOP_DEVICES]

    at_risk: list[dict[str, Any]] = []
    for device in devices:
        at_km = device.get("median_mileage_km")
        if not at_km or at_km > end_km:
            continue
        # 買った時点で既に通過している装置も risk に入れる。「通過済みだから
        # もう安全」ではない。前オーナーが直していれば済んでいるが、
        # 手つかずなら遅れているだけで、むしろ来やすい。記録簿で要確認
        already = at_km <= odometer_km
        at_risk.append({
            "device": device["defective_device"],
            "at_km": at_km,
            "share_pct": device.get("share_pct"),
            "cost_yen": assumptions.repair_cost.get(device["defective_device"],
                                                    REPAIR_COST_DEFAULT),
            "already_passed": already,
        })

    worst = sum(c["cost_yen"] for c in at_risk)
    upcoming = [c for c in at_risk if not c["already_passed"]]
    passed = [c for c in at_risk if c["already_passed"]]
    return {
        "start_km": round(odometer_km),
        "end_km": round(end_km),
        "devices": at_risk,
        "worst_case_yen": worst,
        "reserve_yen": round(worst * assumptions.repair_hit_rate),
        "reserve_per_year_yen": round(worst * assumptions.repair_hit_rate
                                      / assumptions.ownership_years),
        "note": "、".join(f"{c['device']}({c['at_km']:,}km)" for c in upcoming),
        "passed_note": "、".join(f"{c['device']}({c['at_km']:,}km)" for c in passed),
    }


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
    annual_km: float | None = None,
    repair: dict[str, Any] | None = None,
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
    km = annual_km if annual_km else assumptions.annual_km
    fuel = round(km / economy * assumptions.fuel_yen_per_litre)
    repair_reserve = (repair or {}).get("reserve_per_year_yen") or 0

    fixed = (vehicle_tax + weight_tax + compulsory + inspection
             + assumptions.voluntary_insurance + assumptions.maintenance
             + assumptions.parking)
    total = fixed + fuel + repair_reserve + (depreciation or 0)

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
        "annual_km": round(km),
        "annual_km_source": "落札実績" if annual_km else "仮定",
        "odometer_km": (repair or {}).get("start_km"),
        "odometer_end_km": (repair or {}).get("end_km"),
        "repair_devices": (repair or {}).get("note") or "",
        "repair_passed": (repair or {}).get("passed_note") or "",
        "repair_worst_yen": (repair or {}).get("worst_case_yen"),
        "repair_reserve_yen": repair_reserve,
        "is_old_car": age >= OLD_CAR_YEARS,
        # 値落ちを除いた「出ていく現金」。手放すまで実感しないぶんを分けて出す
        "cash_out_yen": fixed + fuel + repair_reserve,
        "total_yen": total,
        "total_manyen": round(total / MANYEN, 1),
        "monthly_yen": round(total / 12),
    }


def build_table(
    listings: list[dict[str, Any]],
    price_rows: list[dict[str, Any]],
    *,
    defects: list[dict[str, Any]] | None = None,
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
    # 不具合は世代で割れているので、車種まるごとのロールアップ行だけ使う
    defects_by_vehicle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in defects or []:
        if row.get("generation") in (None, "", "（車種全体）"):
            defects_by_vehicle[row.get("vehicle_name") or ""].append(row)

    out: list[dict[str, Any]] = []
    for name, rows in sorted(by_vehicle.items()):
        displacement = displacement_of(rows)
        if not displacement:
            continue
        prices = prices_by_vehicle.get(name) or []
        # 価格そのものも、落札が薄い年式は使わない
        points = usable_years(prices)
        median_by_year = dict(points)
        km_per_year = annual_km_of(prices, today_year)
        odometer_by_year = {
            r["model_year"]: r["auction_median_mileage_km"]
            for r in prices
            if r.get("auction_median_mileage_km")
            and (r.get("auction_n") or 0) >= MIN_SAMPLES_PER_YEAR
        }
        # 落札は新しい年式ほど薄い（個人売買に出てこない）。無ければ小売で代える
        retail_by_year = {
            r["model_year"]: r["retail_median_manyen"]
            for r in prices if r.get("retail_median_manyen")
        }
        for age in ages:
            year = today_year - age
            price = median_by_year.get(year) or retail_by_year.get(year)
            if price is None:
                continue
            drop, basis = depreciation_at(year, points)
            odometer = odometer_by_year.get(year)
            if odometer is None and age > 0:
                odometer = (km_per_year or assumptions.annual_km) * age
            repair = None
            if odometer is not None and defects_by_vehicle.get(name):
                repair = repair_outlook(
                    defects_by_vehicle[name], odometer_km=odometer,
                    annual_km=km_per_year or assumptions.annual_km,
                    assumptions=assumptions,
                )
            out.append(estimate(
                vehicle_name=name, model_year=year, price_manyen=price,
                displacement_l=displacement, depreciation_yen=drop,
                depreciation_basis=basis, annual_km=km_per_year, repair=repair,
                assumptions=assumptions, today_year=today_year,
            ))
    out.sort(key=lambda r: (r["vehicle_name"], r["model_year"]))
    return out


# --------------------------------------------------------------- 年次シミュレーション


def simulate(
    *,
    vehicle_name: str,
    model_year: int,
    price_manyen: float,
    displacement_l: float,
    odometer_km: float,
    annual_km: float,
    year_prices: dict[int, float],
    defect_rows: list[dict[str, Any]],
    assumptions: Assumptions,
    years: int = 10,
    today_year: int | None = None,
) -> list[dict[str, Any]]:
    """買ってから 1 年目、2 年目…と積み上げて、累計いくらかかるかを出す。

    1 年ぶんの平均で割るのではなく年ごとに計算するのは、**段差が出る費目が
    あるから**。車検は 2 年に 1 回だし、13 年目・18 年目に税が上がる。
    修理も「その距離に達した年」に来る。均すとそれが見えなくなる。

    値落ちは年式ごとの落札中央値をそのまま辿る。データが無い年式まで来たら
    そこで値落ちは 0 にする（十分古くなれば実際ほぼ止まる）。
    """
    today_year = today_year or date.today().year
    weight = weight_class(displacement_l)
    economy = _bracket(displacement_l,
                       tuple(sorted(assumptions.real_fuel_economy.items())))

    # 装置ごとに「何kmで来るか」。既に通過しているものは 1 年目に置く
    devices = sorted(
        (d for d in defect_rows if (d.get("share_pct") or 0) >= MIN_DEVICE_SHARE_PCT
         and d.get("median_mileage_km")),
        key=lambda d: -(d.get("report_count") or 0),
    )[:TOP_DEVICES]

    # 買った時点で既に発生距離を過ぎている装置は「いつ来てもおかしくない」。
    # 全部 1 年目に積むと初年度だけ跳ね上がって、その後 10 年ずっと修理ゼロと
    # いう嘘の形になる。順番に割り振って、数年かけて片付いていく形にする
    overdue = [d for d in devices if d["median_mileage_km"] <= odometer_km]
    overdue_at_year = {id(d): (i % max(min(len(overdue), years), 1)) + 1
                       for i, d in enumerate(overdue)}

    rows: list[dict[str, Any]] = []
    km = odometer_km
    value = price_manyen
    cumulative = 0
    for t in range(1, years + 1):
        calendar_year = today_year + t
        age = calendar_year - model_year
        km_start, km = km, km + annual_km

        vehicle_tax = annual_vehicle_tax(displacement_l, model_year, calendar_year)
        weight_tax = annual_weight_tax(weight, model_year, calendar_year)
        compulsory = round(COMPULSORY_INSURANCE_24M / 2)
        fuel = round(annual_km / economy * assumptions.fuel_yen_per_litre)
        # 車検は 2 年に 1 回。買った年に受けたとみて 2 年目から
        inspection = assumptions.inspection_fee_2y if t % 2 == 0 else 0

        # その年に通過する装置の修理。1 年目は「既に通過済み」のぶんも見る
        repair = 0
        hit: list[str] = []
        for device in devices:
            at_km = device["median_mileage_km"]
            crossed_now = km_start < at_km <= km
            due_now = overdue_at_year.get(id(device)) == t
            if crossed_now or due_now:
                cost = assumptions.repair_cost.get(device["defective_device"],
                                                   REPAIR_COST_DEFAULT)
                repair += round(cost * assumptions.repair_hit_rate)
                hit.append(device["defective_device"])

        # 値落ちは年式表を 1 年ずつ遡る形で辿る
        next_value = year_prices.get(model_year - (t - 1) - 1)
        depreciation = 0
        if next_value is not None and next_value < value:
            # 年式表は世代交代の段差を含む。1年ぶんの値落ちとして扱えるのは
            # 車両価格の25%まで（単年で3割4割落ちるのはモデルチェンジの段差）
            drop = min(value - next_value, value * DEPRECIATION_CAP)
            depreciation = round(drop * MANYEN)
            value -= drop

        total = (vehicle_tax + weight_tax + compulsory + fuel + inspection
                 + assumptions.voluntary_insurance + assumptions.maintenance
                 + assumptions.parking + repair + depreciation)
        cumulative += total
        rows.append({
            "vehicle_name": vehicle_name,
            "model_year": model_year,
            "year": t,
            "age": age,
            "odometer_km": round(km),
            "vehicle_tax_yen": vehicle_tax,
            "weight_tax_yen": weight_tax,
            "compulsory_insurance_yen": compulsory,
            "voluntary_insurance_yen": assumptions.voluntary_insurance,
            "inspection_yen": inspection,
            "maintenance_yen": assumptions.maintenance,
            "fuel_yen": fuel,
            "repair_yen": repair,
            "repair_devices": "、".join(hit),
            "depreciation_yen": depreciation,
            "total_yen": total,
            "cumulative_yen": cumulative,
            "cumulative_manyen": round(cumulative / MANYEN, 1),
            "is_old_car": age >= OLD_CAR_YEARS,
        })
    return rows


def simulate_table(
    listings: list[dict[str, Any]],
    price_rows: list[dict[str, Any]],
    *,
    defects: list[dict[str, Any]] | None = None,
    ages: tuple[int, ...] = (3, 10),
    years: int = 10,
    assumptions: Assumptions | None = None,
    today_year: int | None = None,
) -> list[dict[str, Any]]:
    """車種 × 年式ぶんの年次シミュレーションをまとめて回す。"""
    assumptions = assumptions or Assumptions()
    today_year = today_year or date.today().year

    by_vehicle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in listings:
        by_vehicle[row.get("vehicle_name") or ""].append(row)
    prices_by_vehicle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in price_rows:
        prices_by_vehicle[row.get("vehicle_name") or ""].append(row)
    defects_by_vehicle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in defects or []:
        if row.get("generation") in (None, "", "（車種全体）"):
            defects_by_vehicle[row.get("vehicle_name") or ""].append(row)

    out: list[dict[str, Any]] = []
    for name, rows in sorted(by_vehicle.items()):
        displacement = displacement_of(rows)
        prices = prices_by_vehicle.get(name) or []
        points = usable_years(prices)
        if not displacement or not points:
            continue
        year_prices = dict(points)
        km_per_year = annual_km_of(prices, today_year) or assumptions.annual_km
        odometer_by_year = {
            r["model_year"]: r["auction_median_mileage_km"]
            for r in prices
            if r.get("auction_median_mileage_km")
            and (r.get("auction_n") or 0) >= MIN_SAMPLES_PER_YEAR
        }
        # 落札は新しい年式ほど薄い（個人売買に出てこないため）。無い年式は
        # 小売の中央値で代える。買値としてはこちらのほうが実態に近くもある
        retail_by_year = {
            r["model_year"]: r["retail_median_manyen"]
            for r in prices if r.get("retail_median_manyen")
        }
        for age in ages:
            model_year = today_year - age
            price = year_prices.get(model_year) or retail_by_year.get(model_year)
            # 走行距離も無ければ「その車種の年間走行 × 車齢」で置く
            odometer = odometer_by_year.get(model_year)
            if odometer is None and age > 0:
                odometer = km_per_year * age
            if price is None or odometer is None:
                continue
            out.extend(simulate(
                vehicle_name=name, model_year=model_year, price_manyen=price,
                displacement_l=displacement, odometer_km=odometer,
                annual_km=km_per_year, year_prices=year_prices,
                defect_rows=defects_by_vehicle.get(name) or [],
                assumptions=assumptions, years=years, today_year=today_year,
            ))
    return out
