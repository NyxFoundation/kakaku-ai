"""**値段を動かしているのはどの変数か**を、在庫の掲載価格から出す。

「走行距離なのか年式なのか」は中古車を探すときに最初に出てくる問いなのに、
一覧を眺めても答えが出ない。年式が新しい個体は走行距離も短いので、
価格差がどちらの効果なのか、見ただけでは切り分けられないため。

ここでやっているのは素の重回帰（最小二乗）1 本だけ。

    支払総額(万円) = a + b1・年式 + b2・走行距離(万km) + b3・7人乗り
                     + b4・修復歴あり + Σ グレードダミー

出すもの:

* **係数** … 他を揃えたとき、その変数が 1 単位動くと価格が何万円動くか
* **標準化係数** … 単位の違いを消した効き具合。**これの絶対値が大きい順が
  「効いている順」**
* **単回帰 R²** … その変数だけで価格のばらつきの何割を説明できるか
* **交換レート** … 「1年新しい ＝ 走行何万km ぶん」。買うときはこれが効く
* **残差** … 回帰が出した推定価格より実売がいくら安いか。割安な個体が浮く

### 注意していること

* **年式と走行距離は強く相関する。** 相関が 0.8 を超えると係数の分離が
  怪しくなる（多重共線性）ので、相関係数も一緒に出して、読む側が
  割り引けるようにしてある。
* **これは掲載価格であって成約価格ではない。** 販売店の売り値の構造を
  見ているだけで、値引き後にいくらで売れたかは入っていない。
* **載っていない変数は係数に紛れ込む。** 色・ワンオーナー・ディーラー系か
  否か・地域はどれも価格を動かすが、一覧ページには出てこない。説明できない
  ぶんは残差に乗るので、R² が 0.6 なら「4割は見えていない何か」と読む。
"""

from __future__ import annotations

import math
import statistics as st
from typing import Any, Sequence

MIN_ROWS_PER_LEVEL = 5  # これ未満しかないカテゴリはダミーを立てない（係数が暴れる）
MIN_ROWS = 30           # これ未満なら回帰そのものを出さない


# --------------------------------------------------------------------- 最小二乗


def _invert(matrix: list[list[float]]) -> list[list[float]] | None:
    """ガウス・ジョルダン法で逆行列。特異なら None。"""
    n = len(matrix)
    aug = [row[:] + [1.0 if i == j else 0.0 for j in range(n)]
           for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-10:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        scale = aug[col][col]
        aug[col] = [v / scale for v in aug[col]]
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            if factor:
                aug[row] = [v - factor * w for v, w in zip(aug[row], aug[col])]
    return [row[n:] for row in aug]


def ols(x_rows: Sequence[Sequence[float]], y: Sequence[float]) -> dict[str, Any] | None:
    """切片つきの最小二乗。係数・標準誤差・t値・R² を返す。

    説明変数が多重共線で正規方程式が解けないときは None（呼ぶ側で列を減らす）。
    """
    n = len(y)
    if n == 0 or not x_rows:
        return None
    k = len(x_rows[0])
    design = [[1.0, *row] for row in x_rows]
    p = k + 1
    if n <= p:
        return None

    xtx = [[sum(design[i][a] * design[i][b] for i in range(n)) for b in range(p)]
           for a in range(p)]
    xty = [sum(design[i][a] * y[i] for i in range(n)) for a in range(p)]
    inv = _invert(xtx)
    if inv is None:
        return None

    beta = [sum(inv[a][b] * xty[b] for b in range(p)) for a in range(p)]
    fitted = [sum(beta[a] * design[i][a] for a in range(p)) for i in range(n)]
    resid = [y[i] - fitted[i] for i in range(n)]
    mean_y = sum(y) / n
    ss_res = sum(r * r for r in resid)
    ss_tot = sum((v - mean_y) ** 2 for v in y)
    if ss_tot <= 0:
        return None

    dof = n - p
    sigma2 = ss_res / dof
    stderr = [math.sqrt(max(sigma2 * inv[a][a], 0.0)) for a in range(p)]
    r2 = 1 - ss_res / ss_tot
    return {
        "n": n,
        "intercept": beta[0],
        "coefficients": beta[1:],
        "stderr": stderr[1:],
        "t_values": [(beta[a] / stderr[a]) if stderr[a] else None for a in range(1, p)],
        "r2": r2,
        "adj_r2": 1 - (1 - r2) * (n - 1) / dof,
        "rmse": math.sqrt(sigma2),
        "fitted": fitted,
        "residuals": resid,
    }


def correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    if len(a) < 3:
        return None
    try:
        return st.correlation(a, b)
    except st.StatisticsError:
        return None


# ------------------------------------------------------------------ 説明変数


def _usable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """価格・年式・走行距離がそろっている行だけ。応談や距離非公開は落とす。"""
    return [r for r in rows
            if r.get("total_price_manyen") and r.get("model_year") and r.get("mileage_km")]


def _grade_levels(rows: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    """ダミーを立てるグレードと、その基準（係数 0 になるグレード）。

    基準は台数が最も多いグレードにする。そうしておくと他の係数が
    「主力グレードに対していくら高いか」として読める。
    """
    counts: dict[str, int] = {}
    for row in rows:
        name = row.get("grade_name") or ""
        if name:
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    usable = [name for name, c in ranked if c >= MIN_ROWS_PER_LEVEL]
    return (usable[0], usable[1:]) if len(usable) >= 2 else (None, [])


def build_design(
    rows: list[dict[str, Any]], *, seats_label: str | None = None
) -> dict[str, Any] | None:
    """在庫の行から説明変数の行列を組む。列の意味も一緒に返す。

    `seats_label` を渡すと `seats_match`（狙いの乗車定員かどうか）をダミーに
    足す。定員は一覧カードに出てこないので、検索側で絞った結果の印を
    収集時に付けてある（`grade_book.collect_by_grade` 参照）。
    """
    data = _usable(rows)
    if len(data) < MIN_ROWS:
        return None

    grade_base, grade_levels = _grade_levels(data)
    seat_flags = [1.0 if r.get("seats_match") else 0.0 for r in data]
    has_seats = bool(seats_label) and (
        MIN_ROWS_PER_LEVEL <= sum(seat_flags) <= len(data) - MIN_ROWS_PER_LEVEL)
    repairs = [1.0 if _repaired(r) else 0.0 for r in data]
    has_repair = MIN_ROWS_PER_LEVEL <= sum(repairs) <= len(data) - MIN_ROWS_PER_LEVEL

    names: list[str] = ["年式（1年新しい）", "走行距離（1万km）"]
    if has_seats:
        names.append(seats_label or "")
    if has_repair:
        names.append("修復歴あり")
    names += [f"グレード: {g}" for g in grade_levels]

    x_rows: list[list[float]] = []
    for row, seat_flag, repair in zip(data, seat_flags, repairs):
        vector = [float(row["model_year"]), row["mileage_km"] / 10_000]
        if has_seats:
            vector.append(seat_flag)
        if has_repair:
            vector.append(repair)
        vector += [1.0 if (row.get("grade_name") or "") == g else 0.0
                   for g in grade_levels]
        x_rows.append(vector)

    return {
        "rows": data,
        "x": x_rows,
        "y": [float(r["total_price_manyen"]) for r in data],
        "names": names,
        "grade_base": grade_base,
    }


def _repaired(row: dict[str, Any]) -> bool:
    """修復歴の申告。「なし」以外（「あり」も空欄も）を 1 とはしない。

    空欄は「あり」ではないので、**申告が「あり」のものだけ**を立てる。
    """
    return "あり" in (row.get("repair_history") or "")


# ------------------------------------------------------------------ 要因ごと


FACTORS: list[tuple[str, Any]] = [
    ("グレード", lambda name: name.startswith("グレード: ")),
    ("年式", lambda name: name.startswith("年式")),
    ("走行距離", lambda name: name.startswith("走行距離")),
    ("修復歴", lambda name: name.startswith("修復歴")),
]


def _factor_of(name: str) -> str:
    """列名を要因にまとめる。どれにも当たらないものは列名そのものを要因名にする
    （乗車定員のように、渡されたラベルがそのまま 1 要因になるもの）。"""
    for factor, matches in FACTORS:
        if matches(name):
            return factor
    return name


def factor_contributions(
    x_rows: list[list[float]], y: list[float], names: list[str], full_r2: float
) -> list[dict[str, Any]]:
    """要因ごとの寄与。その要因の列を全部外して解き直し、R² がいくら落ちるかで測る。

    変数 1 本ずつの係数を並べても「結局どれが効いているのか」は分からない。
    グレードのようにダミーが何本にも割れる要因は、1 本あたりの係数が小さくても
    足し合わせると他を圧倒する、ということが起きるため。
    外したときの説明力の落ち幅なら、本数の違う要因どうしを横に並べて比べられる。
    """
    groups: dict[str, list[int]] = {}
    for index, name in enumerate(names):
        groups.setdefault(_factor_of(name), []).append(index)

    out: list[dict[str, Any]] = []
    for factor, columns in groups.items():
        keep = [i for i in range(len(names)) if i not in set(columns)]
        if keep:
            reduced = ols([[row[i] for i in keep] for row in x_rows], y)
            without = reduced["r2"] if reduced else None
        else:
            without = 0.0  # 説明変数が無くなる＝平均で予測するのと同じ
        out.append({
            "factor": factor,
            "columns": columns,
            "r2_without": without,
            "delta_r2": (full_r2 - without) if without is not None else None,
        })
    out.sort(key=lambda f: -(f["delta_r2"] or 0))
    return out


# -------------------------------------------------------------------- まとめ


def analyse(
    rows: list[dict[str, Any]], *, seats_label: str | None = None
) -> dict[str, Any] | None:
    """重回帰と単回帰をまとめて回して、読める形にして返す。"""
    design = build_design(rows, seats_label=seats_label)
    if not design:
        return None

    x_rows, y, names = design["x"], design["y"], design["names"]
    full = ols(x_rows, y)
    if full is None:
        # 多重共線でこけたらグレードダミーを外して年式と走行距離だけで解き直す
        x_rows = [row[:2] for row in x_rows]
        names = names[:2]
        full = ols(x_rows, y)
        if full is None:
            return None

    sd_y = st.pstdev(y)
    variables = []
    for i, name in enumerate(names):
        column = [row[i] for row in x_rows]
        sd_x = st.pstdev(column)
        simple = ols([[v] for v in column], y)
        coefficient = full["coefficients"][i]
        variables.append({
            "name": name,
            "coefficient": coefficient,
            "stderr": full["stderr"][i],
            "t_value": full["t_values"][i],
            "beta": (coefficient * sd_x / sd_y) if sd_y else None,
            "simple_r2": simple["r2"] if simple else None,
            "simple_coefficient": simple["coefficients"][0] if simple else None,
            "sd": sd_x,
        })
    variables.sort(key=lambda v: -abs(v["beta"] or 0))

    year = next((v for v in variables if v["name"].startswith("年式")), None)
    mileage = next((v for v in variables if v["name"].startswith("走行距離")), None)
    trade_off = None
    if year and mileage and mileage["coefficient"]:
        # 年式の係数 ÷ 走行距離の係数。符号はどちらも「価格を下げる向き」で
        # 揃えるため、年式(+)/走行(-) の比に負号を付けて万km に直す
        trade_off = -year["coefficient"] / mileage["coefficient"]

    data = design["rows"]
    factors = factor_contributions(x_rows, y, names, full["r2"])
    for factor in factors:
        factor["variables"] = [variables[
            next(i for i, v in enumerate(variables) if v["name"] == names[column])]
            for column in factor["columns"]]
    return {
        "n": full["n"],
        "factors": factors,
        "grade_base": design.get("grade_base"),
        "r2": full["r2"],
        "adj_r2": full["adj_r2"],
        "rmse": full["rmse"],
        "intercept": full["intercept"],
        "variables": variables,
        "trade_off_mankm_per_year": trade_off,
        "year_mileage_correlation": correlation(
            [float(r["model_year"]) for r in data],
            [r["mileage_km"] / 10_000 for r in data],
        ),
        "price_median": st.median(y),
        "year_range": (min(r["model_year"] for r in data),
                       max(r["model_year"] for r in data)),
        "mileage_range": (min(r["mileage_km"] for r in data) / 10_000,
                          max(r["mileage_km"] for r in data) / 10_000),
        "fitted_by_listing": {
            r["listing_id"]: (fit, res)
            for r, fit, res in zip(data, full["fitted"], full["residuals"])
        },
    }
