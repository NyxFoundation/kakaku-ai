"""コマンドライン。

    uv run kakaku-ai crawl                 # 今日のスナップショットを取る
    uv run kakaku-ai crawl --only alphard  # 車種を絞る
    uv run kakaku-ai excel                 # 全スナップショットから xlsx を作る
    uv run kakaku-ai upload                # Drive に上げる
    uv run kakaku-ai weekly                # crawl → excel → upload を通しで
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import drive, excel, pipeline, store
from .vehicles import DATA_DIR, load_vehicles

OUTPUT_DIR = DATA_DIR / "xlsx"
OUTPUT_NAME = "toyota_minivan_souba.xlsx"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _output_path(args: argparse.Namespace) -> Path:
    return Path(args.output) if args.output else OUTPUT_DIR / OUTPUT_NAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kakaku-ai", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="スナップショットを取得する")
    crawl.add_argument("--snapshot", help="YYYY-MM-DD（既定=今日）")
    crawl.add_argument("--only", nargs="*", help="車種キーを絞る")
    crawl.add_argument(
        "--sources",
        nargs="*",
        choices=["yahoo", "carsensor", "kakaku", "jmty", "minkara", "mlit", "stock"],
        help="使うソースを絞る",
    )
    crawl.add_argument("--no-cache", action="store_true", help="キャッシュを無視して取り直す")
    crawl.add_argument(
        "--no-detail",
        action="store_true",
        help="ヤフオク商品ページの取得をやめる（年式が埋まらない分が残る）",
    )
    crawl.add_argument(
        "--detail-limit", type=int, help="1車種あたりの商品ページ取得件数の上限（試運転用）"
    )

    xl = sub.add_parser("excel", help="xlsx を組み立てる")
    xl.add_argument("-o", "--output")

    up = sub.add_parser("upload", help="Drive にアップロードする")
    up.add_argument("-o", "--output")
    up.add_argument("--folder-id", default=drive.DEFAULT_FOLDER_ID)
    up.add_argument("--snapshot", help="履歴ファイル名に使う日付（既定=最新）")

    wk = sub.add_parser("weekly", help="crawl → excel → upload を通しで実行")
    wk.add_argument("--only", nargs="*")
    wk.add_argument("-o", "--output")
    wk.add_argument("--folder-id", default=drive.DEFAULT_FOLDER_ID)
    wk.add_argument("--no-upload", action="store_true")

    sub.add_parser("list", help="収録済みスナップショットを表示する")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "crawl":
        pipeline.run(
            args.snapshot,
            only=args.only,
            sources=set(args.sources) if args.sources else None,
            use_cache=not args.no_cache,
            detail=not args.no_detail,
            detail_limit=args.detail_limit,
        )
        return 0

    if args.command == "excel":
        excel.build(_output_path(args))
        return 0

    if args.command == "upload":
        path = _output_path(args)
        if not path.exists():
            raise SystemExit(f"{path} がありません。先に excel を実行してください。")
        snapshots = store.list_snapshots()
        drive.upload(
            path,
            folder_id=args.folder_id,
            snapshot=args.snapshot or (snapshots[-1] if snapshots else None),
        )
        return 0

    if args.command == "weekly":
        snapshot = store.today()
        pipeline.run(snapshot, only=args.only)
        path = excel.build(_output_path(args))
        if not args.no_upload:
            drive.upload(path, folder_id=args.folder_id, snapshot=snapshot)
        return 0

    if args.command == "list":
        vehicles = load_vehicles()
        print(f"車種 {len(vehicles)}: " + ", ".join(v.name for v in vehicles))
        for snap in store.list_snapshots():
            counts = {ds: len(store.read(snap, ds)) for ds in store.DATASETS}
            print(f"{snap}  " + "  ".join(f"{k}={v}" for k, v in counts.items() if v))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
