"""価格.com 自動車から、新車価格帯・中古車価格帯・満足度を取る。

`https://kakaku.com/item/<id>/` 1ページで

* 新車価格 510〜1069 万円 / 発売日
* 中古車価格 45〜1718 万円（9,442 物件）
* 人気ランキング n 位、ボディタイプ内 n 位
* 満足度・レビュー 3.93（投稿数 67 件）、クチコミ 99678 件

が拾える。ページは Shift_JIS。
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from ..http import Fetcher

log = logging.getLogger(__name__)

BASE = "https://kakaku.com/item/{item_id}/"


def _flat_text(raw: str) -> str:
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))


def collect(fetcher: Fetcher, vehicle, snapshot: str) -> dict[str, Any] | None:
    if not vehicle.kakaku_item_id:
        return None

    url = BASE.format(item_id=vehicle.kakaku_item_id)
    text = _flat_text(fetcher.get_text(url, encoding="shift_jis"))

    row: dict[str, Any] = {
        "snapshot_date": snapshot,
        "source": "kakaku_com",
        "vehicle_key": vehicle.key,
        "vehicle_name": vehicle.name,
        "url": url,
    }

    m = re.search(r"新車価格[：:]\s*([\d,]+)\s*[〜~～]\s*([\d,]+)\s*万円", text)
    if m:
        row["new_price_min_manyen"] = int(m.group(1).replace(",", ""))
        row["new_price_max_manyen"] = int(m.group(2).replace(",", ""))

    m = re.search(r"([\d]{4}年\d{1,2}月\d{1,2}日)発売", text)
    if m:
        row["release_date"] = m.group(1)

    m = re.search(
        r"中古車価格[：:]\s*([\d,]+)\s*[〜~～]\s*([\d,]+)\s*万円\s*（\s*([\d,]+)\s*物件", text
    )
    if m:
        row["used_price_min_manyen"] = int(m.group(1).replace(",", ""))
        row["used_price_max_manyen"] = int(m.group(2).replace(",", ""))
        row["used_listing_count"] = int(m.group(3).replace(",", ""))

    m = re.search(r"満足度・レビュー\s*([\d.]+)\s*投稿数[：:]\s*([\d,]+)\s*件", text)
    if m:
        row["satisfaction_score"] = float(m.group(1))
        row["review_count"] = int(m.group(2).replace(",", ""))

    m = re.search(r"クチコミ\s*([\d,]+)\s*件", text)
    if m:
        row["bbs_post_count"] = int(m.group(1).replace(",", ""))

    m = re.search(r"人気ランキング\s*([\d,]+)\s*位\s*(\S+?)\s*([\d,]+)\s*位", text)
    if m:
        row["popularity_rank_overall"] = int(m.group(1).replace(",", ""))
        row["popularity_rank_category"] = m.group(2)
        row["popularity_rank_in_category"] = int(m.group(3).replace(",", ""))

    log.info(
        "  kakaku %s: 新車 %s-%s 万 / 中古 %s-%s 万 / 満足度 %s",
        vehicle.name,
        row.get("new_price_min_manyen"),
        row.get("new_price_max_manyen"),
        row.get("used_price_min_manyen"),
        row.get("used_price_max_manyen"),
        row.get("satisfaction_score"),
    )
    return row
