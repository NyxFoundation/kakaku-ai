"""条件を指定して、実際に買える個体を探す。

`wide` が集めるのは車種×年式の**相場**（集計値）で、買える個体そのものではない。
こちらはカタログから対象車種を選び、カーセンサーの在庫一覧を年式ごとに開いて
**1台ずつ**拾い、状態の良い順に並べる。

    uv run python scripts/find_candidates.py --maker ボルボ --year-from 1990 --year-to 1999
    uv run python scripts/find_candidates.py --body ミニバン --max-price 200 --max-mileage 10
    uv run python scripts/find_candidates.py --model V70 940 240 --sort mileage

### 並べ方

古い車は「安い個体」より「程度の良い個体」を選ぶべきなので、価格だけでは並べない。
既定のスコアは

* **走行距離** — 同年式のなかで少ないほど加点。古い車ではここがいちばん効く
* **修復歴なし** — 加点。「あり」は大きく減点
* **車検残** — 長いほど加点（残がないと10万円前後の出費になる）
* **保証・整備** — 付いていれば加点
* **価格** — 同年式の中央値より安ければ少し加点。ただし**安すぎは減点**にしている。
  30年落ちで極端に安い個体は、状態がそれなりの理由があることが多い

「デザイン」は数値化できないので触らない。グレード・装備の載った出品タイトルと
リンクを出すので、そこは目で見て判断する。
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaku_ai.aggregate import months_until  # noqa: E402
from kakaku_ai.http import Fetcher  # noqa: E402
from kakaku_ai.sources import carsensor_listings as cl  # noqa: E402
from kakaku_ai.wide import load_catalog  # noqa: E402

log = logging.getLogger("find_candidates")


class _V:
    """carsensor_listings に渡すための最小限の車種。"""

    def __init__(self, code: str, name: str) -> None:
        self.key = code
        self.name = name
        self.carsensor_codes = (code,)

    @staticmethod
    def generation_for_model_year(_year):
        return ""


def score(row: dict, peer_median_price: float | None, peer_median_mileage: float | None) -> tuple[float, list[str]]:
    """状態の良さを点数にする。理由も返す（数字だけ出しても判断できないので）。"""
    points = 0.0
    why: list[str] = []

    mileage = row.get("mileage_km")
    if mileage is not None and peer_median_mileage:
        ratio = mileage / peer_median_mileage
        if ratio <= 0.5:
            points += 3; why.append(f"同年式の半分以下の走行（{mileage/10000:.1f}万km）")
        elif ratio <= 0.8:
            points += 1.5; why.append(f"走行が少なめ（{mileage/10000:.1f}万km）")
        elif ratio >= 1.5:
            points -= 1.5; why.append(f"走行が多め（{mileage/10000:.1f}万km）")

    repair = (row.get("repair_history") or "").strip()
    if repair == "なし":
        points += 2; why.append("修復歴なし")
    elif repair == "あり":
        points -= 4; why.append("修復歴あり")

    ym = row.get("inspection_ym") or 0
    left = months_until(ym) or 0
    if left > 0:
        points += min(left / 12, 2)
        why.append(f"車検 {ym // 100}/{ym % 100:02d} まで（残り{left}ヶ月）")
    elif "整備付" in (row.get("inspection") or ""):
        points += 1; why.append("車検整備付")

    if "保証" in (row.get("warranty") or ""):
        points += 1; why.append("保証付")

    price = row.get("total_price_manyen") or row.get("base_price_manyen")
    if price and peer_median_price:
        ratio = price / peer_median_price
        if 0.6 <= ratio <= 0.9:
            points += 1; why.append(f"同年式より安い（中央値 {peer_median_price:.0f}万）")
        elif ratio < 0.5:
            # 30年落ちで極端に安いのは、それなりの理由があることが多い
            points -= 1; why.append("同年式より極端に安い（要確認）")
    return points, why


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--maker", nargs="*", help="メーカー名（例 ボルボ）")
    ap.add_argument("--model", nargs="*", help="車種名の部分一致（例 V70 940）")
    ap.add_argument("--body", nargs="*", help="ボディタイプ（例 ミニバン ステーションワゴン）")
    ap.add_argument("--origin", choices=["国産", "輸入"], help="国産 / 輸入")
    ap.add_argument("--year-from", type=int, default=1990)
    ap.add_argument("--year-to", type=int, default=1999)
    ap.add_argument("--max-price", type=float, help="支払総額の上限（万円）")
    ap.add_argument("--max-mileage", type=float, help="走行距離の上限（万km）")
    ap.add_argument("--sort", choices=["score", "mileage", "price"], default="score")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--pages", type=int, default=2, help="1年式あたりのページ数")
    ap.add_argument("--json", help="結果を JSON で書き出す")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    catalog = load_catalog()
    if not catalog:
        print("カタログが空です。先に `kakaku-ai wide` を回してください。", file=sys.stderr)
        return 1

    targets = []
    for code, meta in catalog.items():
        if args.maker and (meta.get("maker") or "") not in args.maker:
            continue
        if args.origin and meta.get("origin") != args.origin:
            continue
        if args.body and (meta.get("body_type") or "") not in args.body:
            continue
        if args.model and not any(m.lower() in (meta.get("model_name") or "").lower() for m in args.model):
            continue
        targets.append((code, meta))

    if not targets:
        print("条件に合う車種がカタログにありません。", file=sys.stderr)
        return 1
    log.info("対象車種 %s: %s", len(targets), ", ".join(m["model_name"] for _, m in targets[:12]))

    cl.MAX_PAGES_PER_YEAR = args.pages
    fetcher = Fetcher(use_cache=False)
    rows: list[dict] = []
    for n, (code, meta) in enumerate(targets, 1):
        vehicle = _V(code, meta["model_name"])
        found: list[dict] = []
        for year in range(args.year_from, args.year_to + 1):
            try:
                found.extend(cl._fetch_year(fetcher, vehicle, code, year))
            except Exception as exc:  # noqa: BLE001 - 1車種のこけで全体を止めない
                log.warning("  %s %s: %s", meta["model_name"], year, exc)
        for r in found:
            r["model_name"] = meta["model_name"]
            r["maker"] = meta.get("maker")
            r["body_type"] = meta.get("body_type")
        rows.extend(found)
        if found:
            log.info("  [%s/%s] %-22s %s台", n, len(targets), meta["model_name"], len(found))

    if args.max_price:
        rows = [r for r in rows if (r.get("total_price_manyen") or 0) <= args.max_price]
    if args.max_mileage:
        rows = [r for r in rows if (r.get("mileage_km") or 0) <= args.max_mileage * 10_000]

    # 同年式の中央値を出して、そこからの相対で評価する
    by_year: dict[int, list[dict]] = {}
    for r in rows:
        by_year.setdefault(r.get("model_year") or 0, []).append(r)
    med_price = {y: st.median([x["total_price_manyen"] for x in v if x.get("total_price_manyen")])
                 for y, v in by_year.items() if any(x.get("total_price_manyen") for x in v)}
    med_mileage = {y: st.median([x["mileage_km"] for x in v if x.get("mileage_km")])
                   for y, v in by_year.items() if any(x.get("mileage_km") for x in v)}

    for r in rows:
        y = r.get("model_year") or 0
        r["score"], r["why"] = score(r, med_price.get(y), med_mileage.get(y))

    key = {
        "score": lambda r: -r["score"],
        "mileage": lambda r: r.get("mileage_km") or 10**9,
        "price": lambda r: r.get("total_price_manyen") or 10**9,
    }[args.sort]
    rows.sort(key=key)
    rows = rows[: args.limit]

    print(f"\n該当 {len(rows)} 台（{args.year_from}〜{args.year_to}年式）\n")
    for i, r in enumerate(rows, 1):
        mileage = f"{r['mileage_km']/10000:.1f}万km" if r.get("mileage_km") else "距離不明"
        print(f"{i:>2}. {r['model_name']} {r.get('model_year')}年 / {mileage} / "
              f"総額 {r.get('total_price_manyen') or '?'}万円  [score {r['score']:+.1f}]")
        if r.get("title"):
            print(f"    {r['title'][:90]}")
        print(f"    {' ・ '.join(r['why']) if r['why'] else '特記なし'}")
        print(f"    {r['url']}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n{args.json} に書き出しました", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
