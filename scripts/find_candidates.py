"""条件を指定して、実際に買える個体を探す。

`wide` が集めるのは車種×年式の**相場**（集計値）で、買える個体そのものではない。
こちらはカタログから対象車種を選び、カーセンサーの在庫一覧を年式ごとに開いて
**1台ずつ**拾い、状態の良い順に並べる。

    uv run python scripts/find_candidates.py --maker ボルボ --year-from 1990 --year-to 1999
    uv run python scripts/find_candidates.py --body ミニバン --max-price 200 --max-mileage 10
    uv run python scripts/find_candidates.py --model V70 940 240 --sort mileage

並べ方（状態スコア）は `kakaku_ai.classics.rescore()` と同じ。走行距離・修復歴・
車検残・保証を重く、価格は軽く見る。デザインは数値化できないので採点しない。

全メーカーを旧車の年式レンジで一気に舐めて xlsx にするのは `kakaku-ai classics`。
こちらは車種や条件を指定して手元で確かめる用。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaku_ai import store  # noqa: E402
from kakaku_ai.classics import rescore  # noqa: E402
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
    ap.add_argument("--json", help="**絞り込み後の全件**を JSON で書き出す（--limit は表示だけ）")
    ap.add_argument("--no-save", action="store_true",
                    help="拾った在庫を data/carsensor_listings.jsonl に足さない")
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
    # 探索は条件を変えて何度も回すものなので、当日ぶんはキャッシュから返す
    fetcher = Fetcher(use_cache=True)
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
        # 支払総額を出していない店は本体価格で見る。どちらも無い（応談）ものは残す
        rows = [r for r in rows
                if (r.get("total_price_manyen") or r.get("base_price_manyen") or 0) <= args.max_price]
    if args.max_mileage:
        rows = [r for r in rows if (r.get("mileage_km") or 0) <= args.max_mileage * 10_000]

    rescore(rows)  # 車種×年式のなかで相対評価する

    key = {
        "score": lambda r: -r["score"],
        "mileage": lambda r: r.get("mileage_km") or 10**9,
        "price": lambda r: r.get("total_price_manyen") or 10**9,
    }[args.sort]
    rows.sort(key=key)

    # 書き出しは全件。--limit は画面に出す数だけ絞る
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("%s 件を %s に書き出しました", len(rows), args.json)
    if not args.no_save:
        added = cl.record(rows, store.today())
        log.info("在庫ストアに %s 件を新規登録（累計 %s 件）", added, len(cl.load_store()))

    print(f"\n該当 {len(rows)} 台中 上位 {min(args.limit, len(rows))} 台"
          f"（{args.year_from}〜{args.year_to}年式）\n")
    for i, r in enumerate(rows[: args.limit], 1):
        mileage = f"{r['mileage_km'] / 10000:.1f}万km" if r.get("mileage_km") else "距離不明"
        # 支払総額を出していない店もあるので、その場合は車両本体価格で代用する
        if r.get("total_price_manyen"):
            price = f"総額 {r['total_price_manyen']}万円"
        elif r.get("base_price_manyen"):
            price = f"本体 {r['base_price_manyen']}万円（総額表示なし）"
        else:
            price = "価格応談"
        print(f"{i:>2}. {r['model_name']} {r.get('model_year')}年 / {mileage} / "
              f"{price}  [score {r['score']:+.1f}]")
        if r.get("title"):
            print(f"    {r['title'][:90]}")
        print(f"    {' ・ '.join(r['why']) if r['why'] else '特記なし'}")
        print(f"    {r['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
