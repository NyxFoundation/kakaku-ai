"""ヤフオクの取りこぼしを測る。

車種名でキーワード検索したときに、どのカテゴリに何件ヒットしているかを出す。
車種マスタに入れていないカテゴリに実物が埋まっていれば、そこを拾いに行く価値がある。
（逆に、名前が似ているだけの別車種＝タウンエースノア等が混ざるリスクも見える）

    uv run python scripts/check_yahoo_coverage.py
    uv run python scripts/check_yahoo_coverage.py --only granace voxy
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakaku_ai.http import Fetcher  # noqa: E402
from kakaku_ai.sources.yahoo_auction import USED_CAR_CATEGORY, _search  # noqa: E402
from kakaku_ai.vehicles import load_vehicles  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    fetcher = Fetcher(use_cache=False)
    vehicles = load_vehicles()

    for vehicle in vehicles:
        if args.only and vehicle.key not in args.only:
            continue
        wanted = set(vehicle.yahoo_categories)
        counter: Counter[tuple[int, str]] = Counter()
        for item in _search(fetcher, {"auccat": USED_CAR_CATEGORY, "p": vehicle.name}):
            category = item.get("category") or {}
            counter[(category.get("id"), category.get("name"))] += 1

        inside = sum(n for (cid, _), n in counter.items() if cid in wanted)
        outside = [(cid, name, n) for (cid, name), n in counter.most_common() if cid not in wanted]
        print(f"■ {vehicle.name}  マスタ内 {inside}件")
        for cid, name, n in outside:
            print(f"    圏外 {n:>3}件  {name} ({cid})")
        if not outside:
            print("    圏外なし")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
