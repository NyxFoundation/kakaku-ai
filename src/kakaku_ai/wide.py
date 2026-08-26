"""**全車種・全年式**の小売相場を集める。

深掘りしている 20 車種（`config/vehicles.*.yaml`）とは別系統。あちらは
ヤフオク落札・口コミ・不具合まで含めた重い収集で、車種を増やすと現実的な時間に
収まらない。こちらは**カーセンサーの相場ページ 1 枚だけ**を全車種ぶん舐める。

1 ページから

* 車種名 / メーカー / **ボディタイプ（＝用途）** / 生産期間
* 新車時価格・中古車価格レンジ・掲載台数・取扱店舗数
* クチコミ総合評価と 6 軸
* **年式 × 価格帯の度数分布**（＝年式別の相場が出せる）

が全部取れるので、カタログ作りと相場収集を同じ 1 回の取得で兼ねられる。

対象は `https://www.carsensor.net/souba-shashu.xml` に載っている全車種（約 2,200）。
1 件 2 秒で **約 75 分**。週次の本体とは別のタイマーで回す。

### なぜヤフオクを全車種でやらないか

ヤフオクは 1 車種あたり 2 パス × ページ数が要る。2,200 車種だと 5,000〜7,000 リクエスト、
4〜6 時間かかるうえ、車種カテゴリが無い車種も多く空振りが大半になる。
落札実績（＝成約価格）が要るのは実際に検討している車種だけなので、
そこは `config/vehicles.*.yaml` の深掘り対象に絞る。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from .http import Fetcher
from .sources import carsensor
from .vehicles import CONFIG_DIR, DATA_DIR

log = logging.getLogger(__name__)

SITEMAP = "https://www.carsensor.net/souba-shashu.xml"
CATALOG_PATH = DATA_DIR / "catalog.jsonl"
CODE_RE = re.compile(r"/usedcar/souba/([A-Z]+_S\d+)/")


class _Vehicle:
    """`carsensor.collect()` に渡すための最小限の車種オブジェクト。

    深掘り側の `Vehicle` は各サイトの ID を持つ重いものだが、ここでは
    カーセンサーのコードしか要らない。世代の情報も持たないので、
    年式はそのまま年式として扱う。
    """

    def __init__(self, code: str) -> None:
        self.key = code
        self.name = code
        self.carsensor_codes = (code,)

    @staticmethod
    def generation_for_model_year(_year: int | None) -> str:
        return ""

    @staticmethod
    def generation_label(_ym: int | None) -> str:
        return ""


def list_codes(fetcher: Fetcher, prefix: str | None = None) -> list[str]:
    """相場ページのサイトマップから車種コードを全部拾う。"""
    codes = sorted(set(CODE_RE.findall(fetcher.get_text(SITEMAP))))
    if prefix:
        codes = [c for c in codes if c.startswith(f"{prefix}_S")]
    return codes


def load_catalog() -> dict[str, dict[str, Any]]:
    if not CATALOG_PATH.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in CATALOG_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["carsensor_code"]] = row
    return out


def normalize_catalog(catalog: dict[str, dict[str, Any]]) -> int:
    """カタログの取りこぼしを直す。直した件数を返す。

    1. `<メーカー>_S999`（＝そのメーカーの「その他」枠）はページにタイトルが
       無く、メーカー名も車種名も取れない。同じ接頭辞の他の車種から引いて補う。
    2. 国産／輸入は取得時のメーカー名で判定しているので、判定表を直しても
       既存のカタログには反映されない。ここで引き直す。
    """
    from .sources.carsensor import is_domestic

    known: dict[str, str] = {}
    for code, meta in catalog.items():
        prefix = code.split("_S")[0]
        if meta.get("maker") and prefix not in known:
            known[prefix] = meta["maker"]

    fixed = 0
    for code, meta in catalog.items():
        before = (meta.get("maker"), meta.get("origin"))
        if not meta.get("maker"):
            sibling = known.get(code.split("_S")[0])
            if sibling:
                meta["maker"] = sibling
                if meta.get("model_name") in (None, "", code):
                    meta["model_name"] = f"{sibling} その他"
        if meta.get("maker"):
            meta["origin"] = "国産" if is_domestic(meta["maker"]) else "輸入"
        if (meta.get("maker"), meta.get("origin")) != before:
            fixed += 1
    return fixed


def save_catalog(catalog: dict[str, dict[str, Any]]) -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CATALOG_PATH.open("w", encoding="utf-8") as fh:
        for code in sorted(catalog):
            fh.write(json.dumps(catalog[code], ensure_ascii=False) + "\n")


def crawl(
    fetcher: Fetcher,
    snapshot: str,
    *,
    prefix: str | None = None,
    limit: int | None = None,
    model_year_from: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """全車種を舐めて (車種カタログ, 年式別相場) を返す。

    カタログは `data/catalog.jsonl` にも書き出して、次回以降 車種名やメーカーを
    引き直さなくて済むようにする（相場そのものは毎回取り直す）。
    """
    codes = list_codes(fetcher, prefix)
    if limit:
        codes = codes[:limit]
    log.info("全車種クロール: %s車種", len(codes))

    catalog = load_catalog()
    summaries: list[dict[str, Any]] = []
    by_year: list[dict[str, Any]] = []

    for i, code in enumerate(codes, 1):
        vehicle = _Vehicle(code)
        try:
            result = carsensor.collect(fetcher, vehicle, snapshot)
        except Exception as exc:  # noqa: BLE001 - 1車種のこけで全体を止めない
            log.warning("  %s: %s", code, exc)
            continue
        if not result:
            continue

        summary = result["summary"]
        meta = {
            "carsensor_code": code,
            "model_name": summary.get("model_name") or code,
            "maker": summary.get("maker"),
            "origin": "国産" if summary.get("is_domestic") else "輸入",
            "body_type": summary.get("body_type"),
            "production_period": summary.get("production_period"),
        }
        catalog[code] = meta

        summaries.append({**meta, **{k: v for k, v in summary.items() if k != "vehicle_name"},
                          "snapshot_date": snapshot})
        for row in result["by_year"]:
            if model_year_from and row["model_year"] < model_year_from:
                continue
            by_year.append({
                **meta,
                "snapshot_date": snapshot,
                "model_year": row["model_year"],
                "is_open_bucket": row["is_open_bucket"],
                "listing_count": row["listing_count"],
                "retail_median_manyen": row["retail_median_manyen"],
                "retail_mean_manyen": row["retail_mean_manyen"],
                "retail_p25_manyen": row["retail_p25_manyen"],
                "retail_p75_manyen": row["retail_p75_manyen"],
                "url": row["url"],
            })

        if i % 100 == 0:
            log.info("  %s/%s 車種（相場 %s行）", i, len(codes), len(by_year))
            save_catalog(catalog)

    if fixed := normalize_catalog(catalog):
        log.info("  メーカー／国産輸入の欠け %s件を補完", fixed)
    save_catalog(catalog)
    log.info("全車種クロール完了: 車種 %s / 年式別相場 %s行", len(summaries), len(by_year))
    return summaries, by_year


def maker_prefixes(fetcher: Fetcher) -> list[str]:
    """サイトマップに出てくるメーカー接頭辞の一覧。"""
    return sorted({c.split("_S")[0] for c in list_codes(fetcher)})
