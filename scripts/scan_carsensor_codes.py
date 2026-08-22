"""カーセンサーの相場ページコード (XX_Snnn) → 車種名 の対応表を作る。

souba-shashu.xml に載っている全 URL のうち指定メーカー接頭辞のものを順に開き、
<h1> から車種名を拾って JSON に落とす。結果は config/carsensor_codes.json に
コミットしてしまうので、通常のクロールでは再実行しなくてよい。

    uv run python scripts/scan_carsensor_codes.py --prefix TO
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

SITEMAP = "https://www.carsensor.net/souba-shashu.xml"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
OUT = Path(__file__).resolve().parents[1] / "config" / "carsensor_codes.json"


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="TO", help="メーカー接頭辞 (トヨタ=TO)")
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()

    codes = sorted(
        set(re.findall(rf"/usedcar/souba/({args.prefix}_S\d+)/", get(SITEMAP)))
    )
    print(f"{len(codes)} codes for prefix={args.prefix}", file=sys.stderr)

    table: dict[str, str] = {}
    if OUT.exists():
        table = json.loads(OUT.read_text(encoding="utf-8"))

    for i, code in enumerate(codes, 1):
        if code in table:
            continue
        try:
            body = get(f"https://www.carsensor.net/usedcar/souba/{code}/")
        except Exception as exc:  # noqa: BLE001 - 落ちても残りを続ける
            print(f"  {code}: {exc}", file=sys.stderr)
            continue
        m = H1.search(body)
        if not m:
            continue
        # 「アイシスの新車価格・中古車相場」 → 「アイシス」
        name = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        name = re.sub(r"の新車価格.*$", "", name)
        table[code] = name
        print(f"[{i}/{len(codes)}] {code} = {name}", file=sys.stderr)
        time.sleep(args.delay)

    OUT.write_text(
        json.dumps(dict(sorted(table.items())), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT} ({len(table)} entries)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
