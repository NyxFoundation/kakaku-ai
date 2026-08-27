"""みんカラの「車種名 → URL の slug」対応表を作る。

口コミを取るには `/car/<メーカー>/<slug>/review/` の slug が要る。深掘り20車種は
手で書いたが、全車種ぶんを手で調べるのは無理。

幸い **`sitemap_model.xml.gz` に全車種の URL が載っている**。子サイトマップの
`cartop` に 8,698件（うち四輪メーカーぶん 4,358件）。ただし slug はローマ字で、
カーセンサー側の日本語名とは突き合わせられない。

そこで車種トップページを 1 枚ずつ開いて `<title>` から日本語名を取る。

    <title>マツダ ロードスターの口コミ・評価・レビュー｜みんカラ</title>

1 車種 1 リクエストで、四輪ぶん 4,358 件なら約 110 分。**一度作れば使い回せる**
ので `config/minkara_models.json` に保存する。新車種はサイトマップに増えるだけ
なので、差分だけ取りに行けばいい。

なお `include_api/catalog/carselect.aspx` など車種一覧の API は robots.txt で
禁止されているので使わない。
"""

from __future__ import annotations

import gzip
import json
import logging
import re
from typing import Any

from ..http import Fetcher
from ..vehicles import CONFIG_DIR

log = logging.getLogger(__name__)

SITEMAP = "https://minkara.carview.co.jp/sitemap_model.xml.gz"
CAR_TOP = "https://minkara.carview.co.jp/car/{maker}/{slug}/"
INDEX_PATH = CONFIG_DIR / "minkara_models.json"

LOC = re.compile(r"<loc>(\S+?)</loc>")
MODEL_URL = re.compile(r"https://minkara\.carview\.co\.jp/car/([^/]+)/([^/]+)/?$")
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
# 「マツダ ロードスターの口コミ・評価・レビュー｜みんカラ」から車種名だけ取る
TITLE_BODY = re.compile(r"^(\S+)\s+(.+?)の(?:口コミ|クチコミ|レビュー)")

# 二輪メーカーが同じサイトマップに混ざっている。四輪だけ相手にする
CAR_MAKERS = frozenset({
    "toyota", "nissan", "honda", "mazda", "subaru", "mitsubishi", "daihatsu",
    "suzuki", "lexus", "isuzu", "mitsuoka", "hino", "ud",
    "porsche", "bmw", "mercedesbenz", "audi", "volkswagen", "volvo", "peugeot",
    "renault", "citroen", "fiat", "alfaromeo", "lancia", "lotus", "jaguar",
    "landrover", "mini", "abarth", "chevrolet", "ford", "dodge", "chrysler",
    "cadillac", "jeep", "hummer", "tesla", "smart", "opel", "saab", "seat",
    "skoda", "maserati", "ferrari", "lamborghini", "bentley", "rollsroyce",
    "astonmartin", "mclaren", "hyundai", "kia", "byd", "rover", "daimler",
    "morgan", "caterham", "tvr", "delorean", "buick", "gmc", "lincoln",
    "pontiac", "plymouth", "amgeneral", "alpine", "ds", "cupra", "genesis",
})


def list_models(fetcher: Fetcher) -> list[tuple[str, str]]:
    """サイトマップから (メーカーslug, 車種slug) を全部拾う。四輪のみ。"""
    index = gzip.decompress(fetcher.get_bytes(SITEMAP)).decode("utf-8", "replace")
    tops = [u for u in LOC.findall(index) if "cartop" in u]
    pairs: set[tuple[str, str]] = set()
    for url in tops:
        body = gzip.decompress(fetcher.get_bytes(url)).decode("utf-8", "replace")
        for loc in LOC.findall(body):
            m = MODEL_URL.match(loc)
            if m and m.group(1) in CAR_MAKERS:
                pairs.add(m.groups())  # type: ignore[arg-type]
    return sorted(pairs)


def load_index() -> dict[str, dict[str, str]]:
    """`"<メーカー日本語> <車種日本語>"` → {maker_slug, slug, maker_ja, name_ja}。"""
    if not INDEX_PATH.exists():
        return {}
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def save_index(index: dict[str, dict[str, str]]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _japanese_name(html: str) -> tuple[str, str] | None:
    m = TITLE.search(html)
    if not m:
        return None
    body = TITLE_BODY.match(re.sub(r"\s+", " ", m.group(1)).strip())
    return (body.group(1), body.group(2)) if body else None


def build(fetcher: Fetcher, *, limit: int | None = None, refresh: bool = False) -> int:
    """車種トップを開いて日本語名を引き、対応表を育てる。追加した件数を返す。

    既に入っている slug は開き直さない（車種名は変わらないため）。
    `refresh=True` で全部取り直す。
    """
    index = {} if refresh else load_index()
    known = {(v["maker_slug"], v["slug"]) for v in index.values()}

    pairs = [p for p in list_models(fetcher) if p not in known]
    if limit:
        pairs = pairs[:limit]
    log.info("みんカラ対応表: 未取得 %s車種（既知 %s件）", len(pairs), len(known))

    added = 0
    for i, (maker_slug, slug) in enumerate(pairs, 1):
        try:
            html = fetcher.get_text(CAR_TOP.format(maker=maker_slug, slug=slug))
        except Exception as exc:  # noqa: BLE001 - 1件のこけで全体を止めない
            log.warning("  %s/%s: %s", maker_slug, slug, exc)
            continue
        parsed = _japanese_name(html)
        if not parsed:
            continue
        maker_ja, name_ja = parsed
        index[f"{maker_ja} {name_ja}"] = {
            "maker_slug": maker_slug, "slug": slug,
            "maker_ja": maker_ja, "name_ja": name_ja,
        }
        added += 1
        if i % 200 == 0:
            log.info("  %s/%s（%s件そろった）", i, len(pairs), len(index))
            save_index(index)

    save_index(index)
    log.info("みんカラ対応表: %s車種（今回 %s件追加）", len(index), added)
    return added
