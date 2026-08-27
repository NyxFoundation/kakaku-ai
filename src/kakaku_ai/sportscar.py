"""スポーツカーの絞り込みと、落札データの分析。

xlsx そのものは他の本と同じ `books.catalog_book()` で組む（形式を揃えたいので
専用の組み立てはしない）。このモジュールが持つのは 2 つだけ。

* `is_sports()` — どの車種をスポーツカーとして扱うか
* 落札データの分析 — `docs/dealer-discussion.md` に載せる数字を出すためのもの

分析をブックに埋めずドキュメント側に置いているのは、これが
「毎週見る表」ではなく「一度きりの検証結果」だから。効く／効かないが
分かった時点で価値の大半は出ていて、毎週作り直す意味がない。

### なぜヤフオクの落札が効くか

スポーツカーのヤフオクは **出品者の 93〜100% が個人**（実測）。業者の販路では
なく純粋な個人間市場で、店頭とは別の需給で動いている。しかも業者
オークション（USS / TAA）と違って会員でなくても全数が見られる。
それでいて誰も全数を数えていない。

180 日ぶんを取りこぼしなく取っているので、車種 × 年式 × 走行距離で
「いくらで落ちているか」の分布がそのまま出せる。

### 入札上限の出し方（分析用）

年式ごとの落札中央値に、**その年式のなかでの走行距離の効き**を乗せる。
車種をまたいだ回帰はしない（240 の 8万km と V70 の 8万km が別物なのと同じで、
コペンの 5万km と RX-7 の 5万km は意味が違う）。

出すのは 25% / 中央値 / 75% の 3 本。「中央値まで」なら普通に競り負けるし、
「75% まで」出せば大抵は獲れるが高値掴みになる、という幅を見せるため。
1 本の「適正価格」を出すより、幅と件数を見せたほうが判断に使える。

### 終了タイミングについて（先に結論）

「人が見ていない時間に終わる出品は安く獲れる」という話をよく聞くので調べたが、
**この 180 日ぶんのデータでは差が出ていない。**

* 落札の **80% が 18〜24時に終わっている**。そもそも比較できるほど他の時間帯に
  玉が無い（深夜は 21件しかない）
* 曜日別の落札額指数は 95〜105 の範囲に収まり、比較の土台を厳しくすると
  順位が入れ替わる。つまりノイズ

シートにはこの否定的な結果をそのまま載せている。効くと言えないことを
効くように見せるほうが害が大きいため。
"""

from __future__ import annotations

import logging
import statistics as st
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import store
from .charts import _median
from .wide import load_catalog

log = logging.getLogger(__name__)

MANYEN = 10_000

# カーセンサーのボディタイプでは拾いきれない車種。GT-R はセダン扱い、
# ランエボ／WRX もセダン、シビックタイプR はハッチバックに入っている
SPORTS_NAMEPLATES = (
    "GT-R", "RX-7", "RX-8", "スープラ", "MR2", "MR-S", "ロードスター", "シルビア",
    "180SX", "フェアレディZ", "NSX", "S2000", "インテグラ", "タイプR",
    "ランサーエボリューション", "インプレッサ", "WRX", "GTO", "FTO", "セリカ",
    "86", "BRZ", "コペン", "カプチーノ", "ビート", "AZ-1", "アルテッツァ",
    "ロータス", "ケイマン", "ボクスター", "911", "コルベット", "バイパー",
)
SPORTS_BODIES = ("クーペ", "オープン")

# 分布を出すのに最低限ほしい件数。これ未満は「参考」扱いで別に出す
MIN_SAMPLES = 3
# 走行距離の係数はこれ未満だと符号すら当てにならない。実測で、落札 4〜7件だと
# 39% が「走るほど高い」という有り得ない符号になった（15件以上なら 10%）
MIN_MILEAGE_SAMPLES = 10


def is_sports(name: str | None, body_type: str | None) -> bool:
    name = name or ""
    return bool(name) and (
        any(tag in name for tag in SPORTS_NAMEPLATES) or body_type in SPORTS_BODIES
    )


def _body_index() -> dict[tuple[str, str], str]:
    """(メーカー, 車種) → ボディタイプ。ヤフオク側にはボディタイプが無いので補う。"""
    return {
        (m.get("maker") or "", m.get("model_name") or ""): m.get("body_type") or ""
        for m in load_catalog().values()
    }


def collect() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(スポーツカーの落札, スポーツカーの店頭在庫) を保存済みデータから拾う。"""
    body = _body_index()
    auctions, _ = store.read_latest("yahoo_used_cars")
    sports_auctions = [
        r for r in auctions
        if r.get("price") and is_sports(r.get("model_name"),
                                        body.get((r.get("maker") or "", r.get("model_name") or "")))
    ]
    for row in sports_auctions:
        row["body_type"] = body.get((row.get("maker") or "", row.get("model_name") or ""))

    stock, _ = store.read_latest("classic_listings")
    sports_stock = [r for r in stock if is_sports(r.get("model_name"), r.get("body_type"))]
    return sports_auctions, sports_stock


# ------------------------------------------------------- 入札の目安


def _mileage_coefficient(rows: list[dict[str, Any]]) -> float | None:
    """同じ車種・同じ年式のなかで、走行 1万km あたり価格が何 % 変わるか。

    年式をまたぐと「古い＝安い＝よく走っている」が混ざって、走行距離の
    効きを過大に見積もる。年式のなかだけで見る。
    """
    pairs = [(r["mileage_km"] / MANYEN, r["price"]) for r in rows
             if r.get("mileage_km") and r.get("price")]
    if len(pairs) < MIN_MILEAGE_SAMPLES:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var == 0 or my == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pairs) / var
    return round(slope / my * 100, 1)


def bid_guide(auctions: list[dict[str, Any]],
              stock: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """車種 × 年式で「いくらまで入れるか」の目安を作る。"""
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in auctions:
        if row.get("model_year"):
            groups[(row.get("maker") or "", row["model_name"], row["model_year"])].append(row)

    shop: dict[tuple[str, int], list[float]] = defaultdict(list)
    for row in stock:
        price = row.get("total_price_manyen") or row.get("base_price_manyen")
        if price and row.get("model_year"):
            shop[(row.get("model_name") or "", row["model_year"])].append(price)

    out: list[dict[str, Any]] = []
    for (maker, model, year), rows in groups.items():
        prices = sorted(r["price"] / MANYEN for r in rows)
        mileages = [r["mileage_km"] for r in rows if r.get("mileage_km")]
        bids = [r["bid_count"] or 0 for r in rows]
        shop_prices = shop.get((model, year)) or []
        shop_median = _median(shop_prices) if len(shop_prices) >= 2 else None
        median = _median(prices)
        out.append({
            "maker": maker,
            "model_name": model,
            "body_type": rows[0].get("body_type"),
            "model_year": year,
            "n": len(rows),
            "p25_manyen": round(prices[len(prices) // 4], 1),
            "median_manyen": round(median, 1) if median else None,
            "p75_manyen": round(prices[len(prices) * 3 // 4], 1),
            "max_manyen": round(prices[-1], 1),
            "mileage_median_km": int(st.median(mileages)) if mileages else None,
            "mileage_coef_pct": _mileage_coefficient(rows),
            "bid_median": int(st.median(bids)) if bids else None,
            "individual_pct": round(
                sum(1 for r in rows if r.get("seller_is_store") is False) / len(rows) * 100),
            "shop_n": len(shop_prices) or None,
            "shop_median_manyen": round(shop_median, 1) if shop_median else None,
            "shop_premium_pct": (round((shop_median / median - 1) * 100)
                                 if shop_median and median else None),
        })
    out.sort(key=lambda r: (r["model_name"], -(r["model_year"] or 0)))
    return out


# ------------------------------------------------------- 終了タイミング


WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")
SLOTS = ((0, 6, "深夜 0-6時"), (6, 12, "午前 6-12時"),
         (12, 18, "昼 12-18時"), (18, 24, "夜 18-24時"))


def _slot(hour: int) -> str:
    return next(label for lo, hi, label in SLOTS if lo <= hour < hi)


def timing_table(auctions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """終了の (曜日別の落札額指数, 時間帯別の件数) を返す。

    金額をそのまま平均すると「高い車がたまたま日曜に終わった」で歪むので、
    各落札を **同じ車種・同じ年式の中央値で割った比** にしてから集計する。

    比較の土台になる中央値は 5 件以上ある年式からしか取らない。3 件だと
    中央値がその落札自身になりがちで、比が 1.0 に張り付いて差が消える。
    """
    peer: dict[tuple[str, int], list[float]] = defaultdict(list)
    for row in auctions:
        if row.get("model_year") and row.get("price"):
            peer[(row["model_name"], row["model_year"])].append(row["price"])
    medians = {k: st.median(v) for k, v in peer.items() if len(v) >= 5}

    by_weekday: dict[str, list[float]] = defaultdict(list)
    by_slot: dict[str, int] = defaultdict(int)
    for row in auctions:
        if not row.get("end_time"):
            continue
        try:
            ended = datetime.fromisoformat(row["end_time"].replace("Z", "+00:00"))
        except ValueError:
            continue
        by_slot[_slot(ended.hour)] += 1
        base = medians.get((row.get("model_name"), row.get("model_year")))  # type: ignore[arg-type]
        if base:
            by_weekday[WEEKDAYS[ended.weekday()]].append(row["price"] / base)

    weekday_rows = [
        {"weekday": w,
         "n": len(by_weekday.get(w) or []),
         "index": round(st.median(by_weekday[w]) * 100) if by_weekday.get(w) else None}
        for w in WEEKDAYS
    ]
    total = sum(by_slot.values()) or 1
    slot_rows = [
        {"slot": label, "n": by_slot.get(label, 0),
         "share_pct": round(by_slot.get(label, 0) / total * 100, 1)}
        for _, _, label in SLOTS
    ]
    return weekday_rows, slot_rows
