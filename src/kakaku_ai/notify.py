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


# 割安度で色分けする。Slack は attachment の color で左に縦棒が出るので、
# 一覧をスクロールしたときにこれだけで拾える。
COLOR_GREAT = "#2eb886"  # 相場より大幅に安い
COLOR_GOOD = "#ecb22e"   # そこそこ安い
COLOR_PLAIN = "#8d8d8d"  # それ以外（高い側を出しているとき）


def _left(hours: float | None) -> str:
    if hours is None:
        return "不明"
    if hours < 1:
        return "まもなく終了"
    if hours < 24:
        return f"残り{hours:.0f}時間"
    return f"残り{hours / 24:.0f}日"


def _headline(row: dict[str, Any]) -> tuple[str, str, str]:
    """(見出し, 色, 絵文字) を返す。"""
    dev = row.get("deviation_pct")
    if dev is None:
        return "相場比 不明", COLOR_PLAIN, "▫️"
    if dev <= -40:
        return f"相場より {abs(dev):.0f}% 安い", COLOR_GREAT, "🟢"
    if dev < 0:
        return f"相場より {abs(dev):.0f}% 安い", COLOR_GOOD, "🟡"
    return f"相場より {dev:.0f}% 高い", COLOR_PLAIN, "🔺"


def _format(row: dict[str, Any]) -> dict[str, Any]:
    """1 出品を 1 attachment にする。左の色棒＋右にサムネイル。"""
    headline, color, mark = _headline(row)
    hours = row.get("hours_left")
    ending_soon = hours is not None and hours <= 24

    mileage = row.get("mileage_km")
    mileage_s = f"{mileage / 10000:.1f}万km" if mileage else "距離不明"
    seller = "ストア" if row.get("seller_is_store") else "個人"
    current = row.get("current_manyen") or 0
    cur_dev = row.get("current_vs_hammer_pct")
    expected = row.get("expected_manyen")

    # --- 見出し（タイトルはリンク、右にサムネ） ---
    title = row["title"][:64] + ("…" if len(row["title"]) > 64 else "")
    head: dict[str, Any] = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"{mark} *<{row['url']}|{title}>*\n"
                f"{row['vehicle_name']} {row.get('model_year') or '?'}年"
                + (f" {row['generation']}" if row.get("generation") else "")
                + f" ・ {mileage_s}"
            ),
        },
    }
    if row.get("image_url"):
        head["accessory"] = {
            "type": "image",
            "image_url": row["image_url"],
            "alt_text": row["vehicle_name"],
        }

    # --- 数字は 2 列で並べる ---
    if cur_dev is not None:
        rise = "\n_まだ競り上がる_" if row.get("will_rise") else ""
        price_field = f"*現在 {current:.1f}万円*\n落札相場 {expected}万 比 {cur_dev:+.0f}%{rise}"
    else:
        price_field = f"*現在 {current:.1f}万円*"

    fields = [{"type": "mrkdwn", "text": price_field}]
    if row.get("benchmark") == "即決どうし":
        overhead = (row.get("overhead_costs") or 0) / 10_000
        fields.append({
            "type": "mrkdwn",
            "text": f"*即決 {row.get('judge_manyen')}万円*"
                    + (f"（諸費用 {overhead:.1f}万）" if overhead else "")
                    + f"\n即決相場 比 *{headline}*",
        })
    else:
        fields.append({"type": "mrkdwn", "text": f"*判定*\n{headline}"})

    fields.append({
        "type": "mrkdwn",
        "text": f"*{'🔥 ' if ending_soon else ''}{_left(hours)}*\n入札 {row.get('bid_count', 0)}件",
    })
    fields.append({
        "type": "mrkdwn",
        "text": f"*{seller}* 評価{row.get('seller_rating') or '-'}"
                + (f"\n{row['seller_city']}" if row.get("seller_city") else ""),
    })
    body = {"type": "section", "fields": fields}

    blocks: list[dict[str, Any]] = [head, body]

    # --- 注意点は小さめの文字で ---
    context: list[str] = []
    if row.get("risk_strong"):
        context.append("⚠️ " + " ・ ".join(row["risk_strong"]))
    if row.get("risk_notes"):
        context.append("ℹ️ " + " ・ ".join(row["risk_notes"]))
    if row.get("price_basis"):
        context.append(f"📊 {row['price_basis']}")
    if context:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": t} for t in context[:3]],
        })

    return {"color": color, "blocks": blocks, "fallback": f"{row['vehicle_name']} {headline}"}


def post(
    rows: list[dict[str, Any]],
    *,
    channel: str = DEFAULT_CHANNEL,
    header: str | None = None,
    subtitle: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """候補を Slack に流す。1 出品 = 1 attachment（左に割安度の色棒、右にサムネ）。"""
    if not rows:
        log.info("  通知対象なし")
        return {"ok": True, "posted": 0}

    rows = rows[:MAX_PER_RUN]
    top = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header or f"気になる出品 {len(rows)}件", "emoji": True},
        }
    ]
    if subtitle:
        top.append({"type": "context", "elements": [{"type": "mrkdwn", "text": subtitle}]})

    attachments = [_format(r) for r in rows]
    text = f"{header or '気になる出品'} {len(rows)}件: " + " / ".join(
        f"{r['vehicle_name']}{r.get('model_year')}年 {r.get('current_manyen')}万" for r in rows[:3]
    )

    if dry_run:
        log.info("  [dry-run] %s件を %s へ送るところ", len(rows), channel)
        for r in rows:
            head, color, mark = _headline(r)
            log.info("    %s %-8s %s年 現在%s万 %s (%s)", mark, r["vehicle_name"],
                     r.get("model_year"), r.get("current_manyen"), head, r["url"])
        return {"ok": True, "posted": 0, "dry_run": True}

    resp = requests.post(
        POST_URL,
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json; charset=utf-8"},
        json={
            "channel": channel,
            "text": text,
            "blocks": top,
            "attachments": attachments,
            "unfurl_links": False,
            "unfurl_media": False,
        },
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack への投稿に失敗: {data.get('error')}")
    log.info("  Slack へ %s件 投稿 (%s)", len(rows), channel)
    return {"ok": True, "posted": len(rows), "ts": data.get("ts")}
