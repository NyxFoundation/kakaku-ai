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
from collections import Counter
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


def fetch_defects(fetcher: Fetcher, vehicle, snapshot: str, maker: str = "トヨタ") -> list[dict[str, Any]]:
    """ユーザーが国交省に通報した不具合情報を通称名で引く。"""
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


def summarize_defects(
    defects: list[dict[str, Any]], recalls: list[dict[str, Any]], vehicle, snapshot: str
) -> list[dict[str, Any]]:
    """装置ごとに件数を数え、代表事例と対応するリコールを添える。

    「この車の壊れやすい点」シートの中身。
    """
    if not defects:
        return []

    by_device: dict[str, list[dict[str, Any]]] = {}
    for d in defects:
        device = (d.get("defective_device") or "不明").strip()
        by_device.setdefault(device, []).append(d)

    recall_devices = Counter(
        (r.get("defective_device") or "").strip() for r in recalls if r.get("defective_device")
    )
    total = len(defects)

    out: list[dict[str, Any]] = []
    for device, items in sorted(by_device.items(), key=lambda kv: -len(kv[1])):
        mileages = [i["mileage_km"] for i in items if i.get("mileage_km")]
        years = [i["model_year"] for i in items if i.get("model_year")]
        # 症状は長いので、代表として新しい順に 3 件
        examples = sorted(items, key=lambda i: i.get("reception_date") or "", reverse=True)[:3]

        out.append(
            {
                "snapshot_date": snapshot,
                "vehicle_key": vehicle.key,
                "vehicle_name": vehicle.name,
                "defective_device": device,
                "report_count": len(items),
                "share_pct": round(len(items) / total * 100, 1),
                "recall_count_same_device": recall_devices.get(device, 0),
                "median_mileage_km": int(sorted(mileages)[len(mileages) // 2]) if mileages else None,
                "model_year_min": min(years) if years else None,
                "model_year_max": max(years) if years else None,
                "affected_generations": ", ".join(
                    sorted({i["generation"] for i in items if i.get("generation")})
                ),
                "examples": "\n".join(
                    f"[{i.get('reception_date')} {i.get('model_code') or ''} "
                    f"{i.get('first_registration') or ''} "
                    f"{i.get('mileage_km') or '?'}km] {i.get('summary') or ''}"
                    for i in examples
                ),
            }
        )
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
