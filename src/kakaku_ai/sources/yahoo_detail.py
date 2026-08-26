"""ヤフオク! の落札商品ページから、検索結果には出ない詳細スペックを取る。

検索結果 (`closedsearch`) の `carSpec` は年式・走行距離・修復歴だけで、しかも
「中古車・新車」ノードをキーワード検索したときしか付いてこない。
商品ページ (`/jp/auction/<id>`) にはもっと入っている:

    firstRegYear / firstRegMonth   初度登録（= 年式）
    mileage / mileageStatus        走行距離 / 実走行・メーター交換
    grade                          グレード
    repairHistory                  修復歴（あり / なし / わからない）
    bodyType / transmission / fuel / colorTone
    expirationYear / expirationMonth  車検
    totalPrice / totalCosts        諸費用込みの総額

終了から半年経った落札でもページは生きている（実測）。

**結果は `data/auction_details.jsonl` に永続キャッシュする。** 落札済みの商品は
もう変わらないので、一度取ったら二度と取りに行かない。180日窓は毎週重なるから、
これがないと毎週 700 件を取り直すことになる。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..aggregate import inspection_year_month
from ..http import Fetcher
from ..vehicles import DATA_DIR

log = logging.getLogger(__name__)

BASE = "https://auctions.yahoo.co.jp/jp/auction/{auction_id}"
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
STORE_PATH = DATA_DIR / "auction_details.jsonl"

# 説明文に出てきたら整備上の含みがある語。本文そのものは長いので保存せず、
# どれが出たかだけ残す。買い手が個別に確認すべき点を洗い出すのが目的で、
# これ自体が良し悪しの判定ではない。
DESCRIPTION_FLAGS = {
    "現状渡し": ("現状渡し", "現状販売", "ノークレーム", "ノーリターン"),
    "警告灯": ("警告灯", "チェックランプ", "エンジンチェック"),
    "オイル漏れ": ("オイル漏れ", "オイルにじみ", "オイル滲み"),
    "異音": ("異音", "コトコト", "カタカタ", "ガラガラ"),
    "エアコン不調": ("エアコン効かない", "エアコン不良", "エアコン故障"),
    "スライドドア不調": ("スライドドア不良", "スライドドア故障", "パワスラ不良"),
    "修復・板金歴": ("修復歴", "板金", "全塗装", "事故歴"),
    "車検切れ": ("車検切れ", "車検なし", "抹消"),
    "不具合明記": ("不具合", "故障", "要修理", "訳あり"),
}

# 本文（description）は長いうえに相場計算には要らないので保存しない
KEEP = (
    "auction_id",
    "grade",
    "first_reg_year",
    "first_reg_month",
    "mileage_km",
    "mileage_status",
    "repair_history",
    "body_type",
    "transmission",
    "fuel",
    "color",
    "inspection_until",
    "inspection_ym",
    "total_price",
    "total_costs",
    "recycling_deposit",
    "image_count",
    "description_flags",
    "description_len",
    "fetched_from",
)


def load_store() -> dict[str, dict[str, Any]]:
    if not STORE_PATH.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in STORE_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["auction_id"]] = row
    return out


def save_store(store: dict[str, dict[str, Any]]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STORE_PATH.open("w", encoding="utf-8") as fh:
        for auction_id in sorted(store):
            fh.write(json.dumps(store[auction_id], ensure_ascii=False) + "\n")


def _parse(auction_id: str, html: str) -> dict[str, Any] | None:
    m = NEXT_DATA.search(html)
    if not m:
        return None
    try:
        detail = json.loads(m.group(1))["props"]["pageProps"]["initialState"]["item"]["detail"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None

    item = detail.get("item") or {}
    car = item.get("car") or {}
    spec = car.get("spec") or {}
    if not spec:
        return None

    inspection = None
    if spec.get("expirationYear") and spec.get("expirationMonth"):
        # 和暦（令和）で入っている
        inspection = f"R{spec['expirationYear']}/{spec['expirationMonth']:02d}"

    description = str(car.get("description") or "")
    hits = sorted(
        label for label, words in DESCRIPTION_FLAGS.items() if any(w in description for w in words)
    )

    return {
        "auction_id": auction_id,
        "grade": spec.get("grade"),
        "image_count": len(item.get("img") or []),
        "description_flags": hits,
        "description_len": len(description),
        "first_reg_year": spec.get("firstRegYear"),
        "first_reg_month": spec.get("firstRegMonth"),
        "mileage_km": spec.get("mileage"),
        "mileage_status": spec.get("mileageStatus"),
        "repair_history": spec.get("repairHistory"),
        "body_type": spec.get("bodyType"),
        "transmission": spec.get("transmission"),
        "fuel": spec.get("fuel"),
        "color": spec.get("colorTone"),
        "inspection_until": inspection,
        "inspection_ym": inspection_year_month(inspection),
        "total_price": car.get("totalPrice"),
        "total_costs": car.get("totalCosts"),
        "recycling_deposit": car.get("recyclingDeposit"),
        "fetched_from": "detail_page",
    }


def enrich(
    fetcher: Fetcher,
    listings: list[dict[str, Any]],
    *,
    only_missing_year: bool = False,
    limit: int | None = None,
) -> int:
    """落札明細に詳細ページの情報を混ぜる。取得した分だけ件数を返す。

    `only_missing_year=True` なら年式が取れていないものだけを取りに行く。
    既に永続キャッシュにあるものはネットワークを使わない。
    """
    store = load_store()
    targets = [
        r
        for r in listings
        if r.get("auction_id")
        and r["auction_id"] not in store
        and (not only_missing_year or not r.get("model_year"))
    ]
    if limit:
        targets = targets[:limit]

    if targets:
        log.info(
            "  yahoo detail: %s件 取得（キャッシュ済み %s件）", len(targets), len(store)
        )
    for i, row in enumerate(targets, 1):
        auction_id = row["auction_id"]
        try:
            parsed = _parse(auction_id, fetcher.get_text(BASE.format(auction_id=auction_id)))
        except Exception as exc:  # noqa: BLE001 - 1件のこけで止めない
            log.warning("    detail %s: %s", auction_id, exc)
            continue
        # 取れなかった（消えた・車以外）ことも記録して、毎週叩き直さないようにする
        store[auction_id] = parsed or {"auction_id": auction_id, "fetched_from": "unavailable"}
        if i % 50 == 0:
            log.info("    %s/%s", i, len(targets))
            save_store(store)

    if targets:
        save_store(store)

    apply_to(listings, store)
    return len(targets)


def apply_to(listings: list[dict[str, Any]], store: dict[str, dict[str, Any]] | None = None) -> None:
    """永続キャッシュの内容を落札明細にマージする（ネットワークなし）。

    検索結果側で取れている値を正とし、欠けているところだけ詳細で埋める。
    グレードや総額のように詳細にしかない項目はそのまま足す。
    """
    store = load_store() if store is None else store

    for row in listings:
        detail = store.get(row.get("auction_id") or "")
        if not detail or detail.get("fetched_from") != "detail_page":
            continue

        if not row.get("model_year") and detail.get("first_reg_year"):
            year = int(detail["first_reg_year"])
            month = int(detail.get("first_reg_month") or 6)
            row["model_year"] = year
            row["model_year_month"] = year * 100 + month
            row["year_source"] = "detail_page"
        if not row.get("mileage_km") and detail.get("mileage_km"):
            row["mileage_km"] = detail["mileage_km"]
        if not row.get("mileage_type") and detail.get("mileage_status"):
            row["mileage_type"] = (
                "REAL_MILEAGE" if detail["mileage_status"] == "実走行" else "METER_REPLACEMENT"
            )
        if not row.get("repair_type") and detail.get("repair_history"):
            row["repair_type"] = {"なし": "NONE", "あり": "REPAIRED"}.get(
                detail["repair_history"], "UNKNOWN"
            )

        for key in ("grade", "body_type", "transmission", "fuel", "color",
                    "inspection_until", "inspection_ym", "total_price", "total_costs",
                    "image_count", "description_flags", "description_len"):
            if detail.get(key) is not None:
                row.setdefault(key, detail[key])
