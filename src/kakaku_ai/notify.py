"""Slack への通知。

hermes が使っている Bot トークン（`~/.hermes/.env` の `SLACK_BOT_TOKEN`）を
そのまま借りる。投稿先は既定で `#notif-car-auction`。

新着の判定は `data/watch_seen.json` に出した `auction_id` を残しておくだけ。
同じ出品を毎回流さないためで、これがないと通知が読まれなくなる。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import requests

from .vehicles import DATA_DIR

log = logging.getLogger(__name__)

HERMES_ENV = Path.home() / ".hermes" / ".env"
SEEN_PATH = DATA_DIR / "watch_seen.json"
DEFAULT_CHANNEL = "C0BS7MBC3V0"  # #notif-car-auction (Nyx Foundation)
POST_URL = "https://slack.com/api/chat.postMessage"
MAX_PER_RUN = 12  # 1回に流す上限。多すぎると読まれない


def _token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if token:
        return token
    if HERMES_ENV.exists():
        for line in HERMES_ENV.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*SLACK_BOT_TOKEN\s*=\s*(.+)\s*$", line)
            if m:
                return m.group(1).strip().strip("'\"")
    raise RuntimeError(
        "SLACK_BOT_TOKEN が見つかりません（環境変数か ~/.hermes/.env に設定してください）"
    )


# ------------------------------------------------------------------ 既読管理


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")).get("auction_ids", []))


def save_seen(ids: set[str]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(
        json.dumps({"auction_ids": sorted(ids)}, ensure_ascii=False, indent=0) + "\n",
        encoding="utf-8",
    )


# -------------------------------------------------------------------- 整形


def _format(row: dict[str, Any]) -> dict[str, Any]:
    dev = row.get("deviation_pct")
    if dev is None:
        headline = "相場比 不明"
    elif dev <= 0:
        headline = f"相場より {abs(dev):.0f}% 安い"
    else:
        headline = f"相場より {dev:.0f}% 高い"

    seller = "個人" if not row.get("seller_is_store") else "ストア"
    rating = row.get("seller_rating") or "-"
    mileage = row.get("mileage_km")
    mileage_s = f"{mileage/10000:.1f}万km" if mileage else "距離不明"
    total = row.get("judge_manyen") or row.get("current_manyen") or 0
    overhead = (row.get("overhead_costs") or 0) / 10_000

    hours = row.get("hours_left")
    if hours is None:
        left = ""
    elif hours < 24:
        left = f"残り{hours:.0f}時間"
    else:
        left = f"残り{hours/24:.0f}日"

    lines = [
        f"*<{row['url']}|{row['title'][:70]}>*",
        f"{row['vehicle_name']} {row.get('model_year') or '?'}年 "
        f"{row.get('generation') or ''} / {mileage_s} / 出品者: {seller} 評価{rating}"
        + (f" / {row['seller_city']}" if row.get("seller_city") else ""),
    ]

    # いまの入札額が落札相場に対してどこにいるか。これは常に出す。
    current = (row.get("current_manyen") or 0)
    cur_dev = row.get("current_vs_hammer_pct")
    if cur_dev is not None:
        note = "（まだ競り上がる）" if row.get("will_rise") else ""
        lines.append(
            f"現在 *{current:.1f}万円* ← 落札相場 {row.get('expected_manyen')}万円 比 "
            f"*{cur_dev:+.0f}%*{note} ・ 入札 {row.get('bid_count', 0)} ・ {left}"
        )
    else:
        lines.append(f"現在 {current:.1f}万円 ・ 入札 {row.get('bid_count', 0)} ・ {left}")

    # 即決があるなら、即決どうしで比べた結果も出す
    if row.get("benchmark") == "即決どうし":
        lines.append(
            f"即決 *{total:.1f}万円*"
            + (f"（諸費用 {overhead:.1f}万）" if overhead else "")
            + f" ← 即決相場 比 *{headline}*  _{row.get('price_basis')}_"
        )
    else:
        lines.append(
            f"{row.get('judge_kind') or ''} 判定: *{headline}*  _{row.get('price_basis')}_"
        )

    if row.get("risk_strong"):
        lines.append("⚠️ " + " / ".join(row["risk_strong"]))
    if row.get("risk_notes"):
        lines.append("ℹ️ " + " / ".join(row["risk_notes"]))

    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def post(
    rows: list[dict[str, Any]],
    *,
    channel: str = DEFAULT_CHANNEL,
    header: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """候補を Slack に流す。1件1ブロックで、タイトルが出品ページへのリンク。"""
    if not rows:
        log.info("  通知対象なし")
        return {"ok": True, "posted": 0}

    rows = rows[:MAX_PER_RUN]
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header or f"新着 {len(rows)}件", "emoji": True},
        }
    ]
    for row in rows:
        blocks.append(_format(row))
        blocks.append({"type": "divider"})
    blocks.pop()  # 末尾の区切りは要らない

    text = f"{header or '新着'} {len(rows)}件: " + " / ".join(
        f"{r['vehicle_name']} {r.get('model_year')}年 {r.get('current_manyen')}万" for r in rows[:3]
    )

    if dry_run:
        log.info("  [dry-run] %s件を %s へ送るところ", len(rows), channel)
        for r in rows:
            log.info("    %s %s %s万 (%s)", r["vehicle_name"], r.get("model_year"),
                     r.get("current_manyen"), r["url"])
        return {"ok": True, "posted": 0, "dry_run": True}

    resp = requests.post(
        POST_URL,
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json; charset=utf-8"},
        json={"channel": channel, "text": text, "blocks": blocks, "unfurl_links": False},
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack への投稿に失敗: {data.get('error')}")
    log.info("  Slack へ %s件 投稿 (%s)", len(rows), channel)
    return {"ok": True, "posted": len(rows), "ts": data.get("ts")}
