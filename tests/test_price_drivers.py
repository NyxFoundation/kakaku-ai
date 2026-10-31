"""価格要因の回帰が「黙って間違う」部分を押さえるテスト。

最小二乗は入力が悪いと例外を出さずにそれらしい数字を返す。だから
**答えの分かっている合成データで係数が戻ってくるか**を見る。
"""

from __future__ import annotations

from pytest import approx

from kakaku_ai import price_drivers


def test_ols_recovers_known_coefficients() -> None:
    """y = 10 + 3·x1 − 2·x2 をそのまま復元できる。"""
    x_rows = [[float(a), float(b)] for a in range(10) for b in range(10)]
    y = [10 + 3 * a - 2 * b for a, b in x_rows]

    result = price_drivers.ols(x_rows, y)

    assert result is not None
    assert result["intercept"] == approx(10, abs=1e-6)
    assert result["coefficients"][0] == approx(3, abs=1e-6)
    assert result["coefficients"][1] == approx(-2, abs=1e-6)
    assert result["r2"] == approx(1, abs=1e-6)


def test_ols_gives_up_on_collinear_columns() -> None:
    """同じ列を 2 本渡したら None。係数を 2 本に分けようがないため。"""
    x_rows = [[float(i), float(i)] for i in range(20)]
    y = [float(i) for i in range(20)]

    assert price_drivers.ols(x_rows, y) is None


def _stock(index: int, *, year: int, mileage_km: int, grade: str,
           seats_match: bool = True, repair: str = "なし") -> dict:
    """価格は 年式 +10万円/年・走行 −8万円/万km で作る（答えが分かっている）。"""
    price = 300 + 10 * (year - 2018) - 8 * (mileage_km / 10_000)
    return {
        "listing_id": f"AU{index:08d}",
        "model_year": year,
        "mileage_km": mileage_km,
        "total_price_manyen": price,
        "grade_name": grade,
        "seats_match": seats_match,
        "repair_history": repair,
    }


def test_analyse_separates_year_from_mileage() -> None:
    """年式と走行距離が絡んでいても、それぞれの係数に分かれる。

    中古車の在庫は「新しい車ほど走っていない」ので、2 つの変数は必ず相関する。
    単純に年式だけで回すと走行距離の効果まで年式に乗ってしまうので、
    **重回帰でそれが剥がれること**を押さえておく。
    """
    rows = []
    for i, year in enumerate(range(2018, 2025)):
        for j in range(8):
            # 新しい年式ほど走行が短い＝現実と同じ相関のかかり方
            mileage = (2025 - year) * 10_000 + j * 3_000
            rows.append(_stock(i * 10 + j, year=year, mileage_km=mileage,
                               grade="2.7 TX"))

    result = price_drivers.analyse(rows)

    assert result is not None
    by_name = {v["name"]: v for v in result["variables"]}
    assert by_name["年式（1年新しい）"]["coefficient"] == approx(10, abs=1e-6)
    assert by_name["走行距離（1万km）"]["coefficient"] == approx(-8, abs=1e-6)
    # 1年新しい(+10万円) は 走行 1.25万km 少ない(-8万円/万km) ぶんに当たる
    assert result["trade_off_mankm_per_year"] == approx(1.25, abs=1e-6)
    assert result["year_mileage_correlation"] < -0.9


def test_analyse_needs_enough_rows() -> None:
    rows = [_stock(i, year=2020, mileage_km=10_000 * i, grade="2.7 TX")
            for i in range(5)]
    assert price_drivers.analyse(rows) is None


def test_repair_flag_counts_only_declared_damage() -> None:
    """空欄は「修復歴なし」ではないが、「あり」でもない。"""
    assert price_drivers._repaired({"repair_history": "あり"})
    assert not price_drivers._repaired({"repair_history": "なし"})
    assert not price_drivers._repaired({"repair_history": ""})
    assert not price_drivers._repaired({})
