"""スクレイパの「壊れたら黙って間違う」部分を押さえるテスト。

ネットワークは叩かない。ヒューリスティックが効いているところ
（度数分布からの中央値推定、緩い JSON、年式→世代、robots 由来の禁止パラメータ）
だけを対象にしている。
"""

from __future__ import annotations

import json

import pytest

from kakaku_ai import aggregate
from kakaku_ai.sources import carsensor, mlit, yahoo_auction
from kakaku_ai.vehicles import load_vehicles


# ------------------------------------------------------------ カーセンサー


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("20 万円未満", (0.0, 20.0)),
        ("40 万円~", (40.0, 60.0)),
        ("400 万円~", (400.0, 420.0)),
        ("420 万円以上", (420.0, 440.0)),
    ],
)
def test_price_bin(label, expected):
    assert carsensor._parse_price_bin(label) == expected


def test_grouped_stats_interpolates_within_bin():
    # 20-40 に 1 台、40-60 に 3 台 → 中央値は 40-60 のビンの中に落ちる
    stats = carsensor._grouped_stats([((20.0, 40.0), 1), ((40.0, 60.0), 3)])
    assert stats["n"] == 4
    assert 40.0 <= stats["median"] <= 60.0
    assert stats["mean"] == pytest.approx((30 * 1 + 50 * 3) / 4)


def test_grouped_stats_empty():
    assert carsensor._grouped_stats([])["n"] == 0
    assert carsensor._grouped_stats([((0.0, 20.0), 0)])["median"] is None


def test_cross_table_column_alignment():
    """年式列と <td> が 1 対 1 で対応していること。

    ここがずれると全部の年式相場が 1 年分ずれる。実際に一度やらかした。
    """
    from bs4 import BeautifulSoup

    html = """
    <table class="defaultTable__table">
      <thead>
        <tr><th colspan="2">中古車情報の相場</th><th>2013年</th><th>2014年</th></tr>
        <tr><th colspan="2">合計 5 台</th><th>2 台</th><th>3 台</th></tr>
      </thead>
      <tbody>
        <tr><th>40 万円~</th><th>4 台</th><td>2</td><td>2</td></tr>
        <tr><th>60 万円~</th><th>1 台</th><td>&nbsp;</td><td>1</td></tr>
      </tbody>
    </table>
    """
    table = BeautifulSoup(html, "lxml").select_one("table")
    columns, rows = carsensor._parse_cross_table(table)

    assert columns == ["2013年", "2014年"]
    # 2013年 列 = 台数 2（40万円台のみ）、2014年 列 = 台数 3
    assert sum(counts[0] for _, counts in rows) == 2
    assert sum(counts[1] for _, counts in rows) == 3


# ------------------------------------------------------------------ 国交省


def test_loads_lenient_handles_trailing_commas():
    """連ラクダの CGI はテンプレート由来の末尾カンマ付き JSON を返す。"""
    raw = """
        \n\n
        { "data": [ {"a": 1}, {"b": 2}, ]
        }
        \n
    """
    assert mlit.loads_lenient(raw) == {"data": [{"a": 1}, {"b": 2}]}


def test_loads_lenient_rejects_garbage():
    with pytest.raises(ValueError):
        mlit.loads_lenient("no json here")


def test_recall_types_gathers_numbered_lists():
    row = {
        "typeList": [{"recall_type_data_car_mlit_model_name": "DBA-AGH30W"}],
        "typeList3": [{"recall_type_data_car_mlit_model_name": "DBA-AGH35W"}],
        "typeList7": [],
        "other": "無視される",
    }
    models = {t["recall_type_data_car_mlit_model_name"] for t in mlit._recall_types(row)}
    assert models == {"DBA-AGH30W", "DBA-AGH35W"}


def test_normalize_recalls_matches_by_bare_model_code():
    """排ガス記号が違っても素の型式が一致すれば拾う（3BA- と DBA- など）。"""
    vehicle = load_vehicles().by_key("alphard")
    rows = [
        {
            "recall_data_car_mlit_notification_no": "1",
            "recall_data_car_mlit_notification_date": "2025/01/22",
            "recall_data_car_mlit_defective_device": "燃料ポンプ",
            "recall_data_car_mlit_recall_car_count": "54,577",
            "typeList": [
                {
                    "recall_type_data_car_mlit_model_name": "5BA-AGH30W",
                    "recall_type_data_car_mlit_common_name": "アルファード",
                }
            ],
        },
        {
            "recall_data_car_mlit_notification_no": "2",
            "recall_data_car_mlit_defective_device": "無関係",
            "typeList": [
                {
                    "recall_type_data_car_mlit_model_name": "DBA-ZRR80W",
                    "recall_type_data_car_mlit_common_name": "ノア",
                }
            ],
        },
    ]
    out = mlit.normalize_recalls(rows, vehicle, "2026-08-22")
    assert [r["notification_no"] for r in out] == ["1"]
    assert out[0]["target_units"] == 54577


# ------------------------------------------------------------------ ヤフオク


def test_forbidden_params_are_rejected():
    """robots.txt が禁じているパラメータを付けようとしたら止まること。"""
    with pytest.raises(ValueError, match="robots.txt"):
        yahoo_auction._check_params({"auccat": 26360, "n": 100})
    yahoo_auction._check_params({"auccat": 26360, "b": 51, "p": "アルファード"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(20240401, 202404), ("20150131", 201501), (None, None), (0, None), (12, None)],
)
def test_model_year_month(raw, expected):
    assert yahoo_auction._model_year_month(raw) == expected


def test_next_data_missing_raises():
    with pytest.raises(ValueError):
        yahoo_auction._parse_next_data("<html><body>no script</body></html>")


def test_next_data_roundtrip():
    payload = {"props": {"pageProps": {}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
    assert yahoo_auction._parse_next_data(html) == payload


# ------------------------------------------------------------------ 集計


class _FakeVehicle:
    key = "test"
    name = "テスト車"

    @staticmethod
    def generation_label(_ym):
        return "1系"


def test_yahoo_by_year_excludes_tampered_and_repaired():
    rows = [
        {"model_year": 2020, "price": 1_000_000, "mileage_type": "REAL_MILEAGE", "repair_type": "NONE", "mileage_km": 50_000, "bid_count": 3},
        {"model_year": 2020, "price": 2_000_000, "mileage_type": "REAL_MILEAGE", "repair_type": "NONE", "mileage_km": 30_000, "bid_count": 5},
        # メーター交換 → 主系列から外れる
        {"model_year": 2020, "price": 100_000, "mileage_type": "METER_REPLACEMENT", "repair_type": "NONE", "mileage_km": 200_000, "bid_count": 1},
    ]
    out = aggregate.yahoo_by_year(rows, _FakeVehicle(), "2026-08-22")
    assert len(out) == 1
    assert out[0]["auction_n"] == 2
    assert out[0]["auction_median_manyen"] == pytest.approx(150.0)
    assert out[0]["excluded_n"] == 1
    assert out[0]["basis"] == "実走行・修復歴なし"


def test_yahoo_by_year_falls_back_when_all_flagged():
    rows = [
        {"model_year": 2019, "price": 500_000, "mileage_type": "METER_REPLACEMENT", "repair_type": "NONE"},
    ]
    out = aggregate.yahoo_by_year(rows, _FakeVehicle(), "2026-08-22")
    assert out[0]["auction_n"] == 1
    assert out[0]["basis"].startswith("全件")


def test_merge_price_rows_computes_premium_and_filters_old_years():
    auction = [
        {"model_year": 2012, "auction_median_manyen": 50.0, "auction_n": 3, "generation": "1系"},
        {"model_year": 2020, "auction_median_manyen": 100.0, "auction_n": 4, "generation": "1系"},
    ]
    retail = [
        {"model_year": 2020, "retail_median_manyen": 150.0, "listing_count": 20, "generation": "1系"},
        {"model_year": 2021, "retail_median_manyen": 180.0, "listing_count": 10, "generation": "1系"},
    ]
    out = aggregate.merge_price_rows(
        auction, retail, _FakeVehicle(), "2026-08-22", model_year_from=2013
    )
    years = [r["model_year"] for r in out]
    assert years == [2020, 2021]  # 2012 は対象外

    row2020 = out[0]
    assert row2020["retail_minus_auction_manyen"] == pytest.approx(50.0)
    assert row2020["retail_premium_pct"] == pytest.approx(50.0)

    # 片側しかない年は乖離を出さない
    assert out[1]["retail_premium_pct"] is None


# ------------------------------------------------------------ 車種マスタ


def test_generation_resolution_at_boundaries():
    alphard = load_vehicles().by_key("alphard")
    assert alphard.generation_label(201412) == "20系"
    assert alphard.generation_label(201501) == "30系前期"  # 境界は from 側に含む
    assert alphard.generation_label(201711) == "30系前期"
    assert alphard.generation_label(201712) == "30系後期"
    assert alphard.generation_label(202306) == "40系"
    assert alphard.generation_label(None) == ""


def test_every_vehicle_has_ids():
    for vehicle in load_vehicles():
        assert vehicle.generations, f"{vehicle.name}: 世代が未設定"
        assert vehicle.all_models, f"{vehicle.name}: 型式が未設定"
        assert vehicle.carsensor_codes, f"{vehicle.name}: カーセンサーのコードが引けていない"
        assert vehicle.minkara_slug, f"{vehicle.name}: みんカラの slug が未設定"


# ------------------------------------------------------------------ みんカラ


def test_review_summary_dedupes_identical_text():
    """別レビューでも本文が丸かぶりのことがある（みんカラに実在）。

    URL が違うので個票としては別物だが、代表コメントに同じ文を並べても意味がない。
    """
    from kakaku_ai.sources import minkara

    rows = [
        {"model_year": 2008, "score_overall": 4, "generation": "50系",
         "good_points": "車内広く使える", "bad_points": ""},
        {"model_year": 2008, "score_overall": 5, "generation": "50系",
         "good_points": "車内広く使える", "bad_points": ""},
        {"model_year": 2008, "score_overall": 3, "generation": "50系",
         "good_points": "3列目シートが床下収納になる", "bad_points": "燃費が悪い"},
    ]
    out = minkara.summarize(rows, _FakeVehicle(), "2026-08-22")
    assert len(out) == 1
    assert out[0]["review_count"] == 3
    assert out[0]["good_points"] == "車内広く使える / 3列目シートが床下収納になる"
    assert out[0]["bad_points"] == "燃費が悪い"
    assert out[0]["score_overall"] == pytest.approx(4.0)
