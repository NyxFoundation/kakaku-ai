"""国土交通省「自動車不具合情報ホットライン（連ラクダ）」から

* リコール届出情報      (class=recalldatacar)
* ユーザー通報の不具合情報 (class=releasedatacar)

を取る。整備観点の「壊れやすい点」の一次ソース。

画面は SPA だが、裏の MovableType Estraier CGI をそのまま叩ける。
返ってくる JSON はテンプレート由来の**末尾カンマ**が混ざる緩い JSON なので、
`loads_lenient()` で直してから読む。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from ..http import Fetcher

log = logging.getLogger(__name__)

ENDPOINT = "https://renrakuda.mlit.go.jp/mt/mt-estraier.cgi"
BLOG_ID = 4
PAGE_SIZE = 50
MAX_PAGES = 60

TRAILING_COMMA = re.compile(r",\s*([}\]])")


def loads_lenient(text: str) -> dict[str, Any]:
    """テンプレートが吐く空白まみれ・末尾カンマありの JSON を読む。"""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("JSON が見つかりません")
    body = text[start : end + 1]
    body = TRAILING_COMMA.sub(r"\1", body)
    return json.loads(body)


def _paginate(fetcher: Fetcher, params: dict[str, Any], order_by: str) -> Iterator[dict[str, Any]]:
    for page in range(MAX_PAGES):
        query = dict(params)
        query.update(
            {
                "blog_id": BLOG_ID,
                "offset": page * PAGE_SIZE + 1,
                "limit": PAGE_SIZE,
                "order_by": order_by,
                "order_condition": "STRD",
            }
        )
        payload = loads_lenient(fetcher.get_text(ENDPOINT, query))
        rows = payload.get("data") or []
        if not rows:
            return
        yield from rows
        if len(rows) < PAGE_SIZE:
            return


# --------------------------------------------------------------------- recall


def fetch_recalls(fetcher: Fetcher, maker: str = "トヨタ") -> list[dict[str, Any]]:
    """メーカー単位でリコール届出を全件取る。

    リコール検索は型式（排ガス記号込み完全一致）でしか絞れないので、型式を当てにいく
    より全件引いてから `typeList` の通称名でローカルに振り分けるほうが確実。
    """
    rows = list(
        _paginate(
            fetcher,
            {
                "class": "recalldatacar",
                "car_name_code": maker,
                "notification_date": "1990/01/01 2099/12/31",
            },
            "recall_data_car_mlit_notification_date",
        )
    )
    log.info("  mlit recall %s: %s件", maker, len(rows))
    return rows


def _recall_types(row: dict[str, Any]) -> list[dict[str, Any]]:
    """typeList / typeList1..60 に散らばる型式情報をまとめる。"""
    out: list[dict[str, Any]] = []
    for key, value in row.items():
        if key == "typeList" or re.fullmatch(r"typeList\d+", key):
            if isinstance(value, list):
                out.extend(v for v in value if isinstance(v, dict))
    return out


def normalize_recalls(
    rows: list[dict[str, Any]], vehicle, snapshot: str
) -> list[dict[str, Any]]:
    """全件のリコールから、この車種に該当するものだけを取り出す。"""
    wanted_names = {n for n in vehicle.mlit_common_names}
    wanted_models = {m.upper() for m in vehicle.all_models}
    # 排ガス記号を落とした素の型式（AGH30W 等）でも拾えるようにする
    wanted_bare = {m.split("-")[-1].upper() for m in wanted_models}

    out: list[dict[str, Any]] = []
    for row in rows:
        types = _recall_types(row)
        matched = []
        for t in types:
            common = (t.get("recall_type_data_car_mlit_common_name") or "").strip()
            model = (t.get("recall_type_data_car_mlit_model_name") or "").strip().upper()
            bare = model.split("-")[-1]
            if common in wanted_names or model in wanted_models or bare in wanted_bare:
                matched.append(t)
        if not matched:
            continue

        out.append(
            {
                "snapshot_date": snapshot,
                "source": "mlit_recall",
                "vehicle_key": vehicle.key,
                "vehicle_name": vehicle.name,
                "notification_no": row.get("recall_data_car_mlit_notification_no"),
                "notification_date": row.get("recall_data_car_mlit_notification_date"),
                "defective_device": row.get("recall_data_car_mlit_defective_device"),
                "target_units": _to_int(row.get("recall_data_car_mlit_recall_car_count")),
                "production_from": row.get("recall_data_car_mlit_import_production_start_date"),
                "production_to": row.get("recall_data_car_mlit_import_production_end_date"),
                "situation": _clean(row.get("recall_data_car_mlit_situation_explanatory_text")),
                "measures": _clean(row.get("recall_data_car_mlit_measures_explanatory_text")),
                "models": ", ".join(
                    sorted(
                        {
                            (t.get("recall_type_data_car_mlit_model_name") or "").strip()
                            for t in matched
                        }
                        - {""}
                    )
                ),
                "common_names": ", ".join(
                    sorted(
                        {
                            (t.get("recall_type_data_car_mlit_common_name") or "").strip()
                            for t in matched
                        }
                        - {""}
                    )
                ),
            }
        )
    return out


# --------------------------------------------------------------------- defect


def fetch_defects(fetcher: Fetcher, vehicle, snapshot: str, maker: str = "トヨタ",
                  limit: int | None = None) -> list[dict[str, Any]]:
    """ユーザーが国交省に通報した不具合情報を通称名で引く。

    `limit` を渡すとその件数で打ち切る。受付が新しい順に返ってくるので、
    残るのは直近ぶん。全車種を舐めるときに要る。セレナは 2,362件（48ページ）
    あって、これを 1,000車種でやると 20 時間かかる。装置ごとの構成比と
    発生時の走行距離の中央値を出すだけなら数百件で十分ぶれない。

    **打ち切ったら件数はもう「通報の総数」ではない。** 呼び出し側で
    そうと分かるようにすること（`truncated` を立てている）。
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for common_name in vehicle.mlit_common_names:
        for row in _paginate(
            fetcher,
            {
                "class": "releasedatacar",
                "release_data_car_mlit_reception_date": "0000-00-00 9999-12-31",
                "release_data_car_mlit_car_name": maker,
                "release_data_car_mlit_car_common_name": common_name,
            },
            "release_data_car_mlit_reception_date",
        ):
            control_no = row.get("release_data_car_mlit_control_no") or row.get("id")
            if not control_no or control_no in seen:
                continue
            seen.add(control_no)

            reg = (row.get("release_data_car_mlit_initial_registration_date") or "").strip()
            year = None
            ym = None
            m = re.match(r"(\d{4})[/-](\d{1,2})", reg)
            if m:
                year = int(m.group(1))
                ym = year * 100 + int(m.group(2))

            if limit and len(rows) >= limit:
                log.info("  mlit defect %s: %s件で打ち切り（上限）", vehicle.name, limit)
                return rows

            rows.append(
                {
                    "snapshot_date": snapshot,
                    "source": "mlit_defect",
                    "vehicle_key": vehicle.key,
                    "vehicle_name": vehicle.name,
                    "control_no": control_no,
                    "reception_date": row.get("release_data_car_mlit_reception_date"),
                    "defective_device": row.get("release_data_car_mlit_defective_device"),
                    "common_name": row.get("release_data_car_mlit_car_common_name"),
                    "model_code": row.get("release_data_car_mlit_model"),
                    "engine_model": row.get("release_data_car_mlit_prime_mover_model"),
                    "first_registration": reg,
                    "model_year": year,
                    "generation": vehicle.generation_label(ym),
                    "mileage_km": _to_int(row.get("release_data_car_mlit_total_mileage")),
                    "emergence_time": row.get("release_data_car_mlit_emergence_time"),
                    "summary": _clean(row.get("release_data_car_mlit_report_content_summary")),
                    "prefecture": row.get("release_data_car_mlit_prefectures"),
                }
            )

    log.info("  mlit defect %s: %s件", vehicle.name, len(rows))
    return rows


ROLLUP = "（車種全体）"


def summarize_defects(
    defects: list[dict[str, Any]], recalls: list[dict[str, Any]], vehicle, snapshot: str
) -> list[dict[str, Any]]:
    """世代 × 装置で件数を数え、代表事例と対応するリコールを添える。

    「この車の壊れやすい点」シートの中身。年式そのままだと 1 件ずつに散って
    読めなくなるので、車種マスタの世代でまとめる。あわせて車種全体の
    ロールアップ行（世代 = `（車種全体）`）も出すので、
    ざっくり見たいときはそこだけ拾えばいい。
    """
    if not defects:
        return []

    def build(generation: str, items: list[dict[str, Any]], denominator: int) -> list[dict[str, Any]]:
        by_device: dict[str, list[dict[str, Any]]] = {}
        for d in items:
            by_device.setdefault((d.get("defective_device") or "不明").strip(), []).append(d)

        rows: list[dict[str, Any]] = []
        for device, group in sorted(by_device.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            mileages = sorted(i["mileage_km"] for i in group if i.get("mileage_km"))
            years = [i["model_year"] for i in group if i.get("model_year")]
            # 症状は長いので、代表として受付が新しい順に 3 件
            examples = sorted(group, key=lambda i: i.get("reception_date") or "", reverse=True)[:3]

            rows.append(
                {
                    "snapshot_date": snapshot,
                    "vehicle_key": vehicle.key,
                    "vehicle_name": vehicle.name,
                    "generation": generation,
                    "defective_device": device,
                    "report_count": len(group),
                    "share_pct": round(len(group) / denominator * 100, 1) if denominator else None,
                    "median_mileage_km": mileages[len(mileages) // 2] if mileages else None,
                    "model_year_min": min(years) if years else None,
                    "model_year_max": max(years) if years else None,
                    "model_codes": ", ".join(
                        sorted({i["model_code"] for i in group if i.get("model_code")})
                    ),
                    "examples": "\n".join(
                        f"[{i.get('reception_date')} {i.get('model_code') or ''} "
                        f"{i.get('first_registration') or ''} "
                        f"{i.get('mileage_km') or '?'}km] {i.get('summary') or ''}"
                        for i in examples
                    ),
                }
            )
        return rows

    out = build(ROLLUP, defects, len(defects))

    by_generation: dict[str, list[dict[str, Any]]] = {}
    for d in defects:
        by_generation.setdefault(d.get("generation") or "不明", []).append(d)

    # 世代は車種マスタの並び順（新しい順）で出す
    order = {gen.code: i for i, gen in enumerate(vehicle.generations)}
    for generation in sorted(by_generation, key=lambda g: order.get(g, 999)):
        items = by_generation[generation]
        out.extend(build(generation, items, len(items)))

    return out


# --------------------------------------------------------------------- helpers


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    m = re.search(r"\d[\d,]*", str(value))
    return int(m.group(0).replace(",", "")) if m else None


def _clean(value: Any) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", "", str(value))
    return re.sub(r"[\s　]+", " ", text).strip()
