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

from . import drive, excel, notify, pipeline, store, watch
from .vehicles import DATA_DIR, load_vehicles

OUTPUT_DIR = DATA_DIR / "xlsx"
OUTPUT_NAME = "minivan_souba.xlsx"


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

    wa = sub.add_parser("watch", help="出品中のオークションを監視して Slack に通知する")
    wa.add_argument("--only", nargs="*", help="車種キーを絞る")
    wa.add_argument("--budget", type=float, help="支払総額の上限（万円）")
    wa.add_argument("--individual-only", action="store_true", help="個人出品だけに絞る")
    wa.add_argument("--channel", default=notify.DEFAULT_CHANNEL)
    wa.add_argument("--dry-run", action="store_true", help="Slack に送らず内容だけ表示")
    wa.add_argument("--all", action="store_true", help="既に通知したものも対象にする")
    wa.add_argument("--year-from", type=int, help="対象年式の下限（既定=車種マスタの設定）")
    wa.add_argument("--no-detail", action="store_true", help="商品ページを開かない（写真枚数と説明文の記載を省く）")
    wa.add_argument(
        "--repair",
        choices=watch.REPAIR_MODES,
        default="none",
        help="修復歴の絞り方。none=申告が「なし」のものだけ（既定） / any=絞らない",
    )

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

    if args.command == "watch":
        from .http import Fetcher
        from .sources import yahoo_open

        models, defects, vehicles = watch.load_context()
        targets = [v for v in vehicles if (not args.only or v.key in args.only) and v.yahoo_categories]
        fetcher = Fetcher(use_cache=False)

        listings: list[dict] = []
        snapshot = store.today()
        for vehicle in targets:
            try:
                listings.extend(yahoo_open.collect(fetcher, vehicle, snapshot))
            except Exception as exc:  # noqa: BLE001 - 1車種のこけで止めない
                logging.getLogger(__name__).error("  %s: %s", vehicle.name, exc)

        picked = watch.pick(
            watch.evaluate(listings, models, defects),
            budget_manyen=args.budget,
            individual_only=args.individual_only,
            model_year_from=args.year_from or vehicles.model_year_from,
            repair=args.repair,
        )

        seen = set() if args.all else notify.load_seen()
        fresh = [r for r in picked if r["auction_id"] not in seen]

        # 通知する分だけ商品ページを開いて、写真の枚数と説明文の記載を足す。
        # 一覧には入っていない情報で、拾うと注意点がだいぶ具体的になる。
        # 全件やると重いので、実際に流す上限のぶんだけ。
        head = fresh[: notify.MAX_PER_RUN]
        if head and not args.no_detail:
            from .sources import yahoo_detail

            yahoo_detail.enrich(Fetcher(use_cache=False), head)
            head = watch.evaluate(head, models, defects)
            # 一覧に修復歴が無かったものは、商品ページで分かった値で判定し直す
            before = len(head)
            head = [r for r in head if watch.repair_ok(r, args.repair, resolved=True)]
            if len(head) < before:
                logging.getLogger(__name__).info(
                    "  修復歴の確認で %s件 除外", before - len(head)
                )
            fresh = head + fresh[notify.MAX_PER_RUN :]
        logging.getLogger(__name__).info(
            "出品 %s件 → 該当 %s件 → 未通知 %s件", len(listings), len(picked), len(fresh)
        )

        result = notify.post(fresh, channel=args.channel, dry_run=args.dry_run,
                             header=f"気になる出品 {len(fresh)}件")
        if not args.dry_run:
            # 実際に流したものだけを既読にする。スキャンした全件を既読にすると、
            # 今日はまだ競り上がり前で判定保留だった出品が、終了間際になっても
            # 二度と通知されなくなる。
            notify.save_seen(seen | {r["auction_id"] for r in fresh[: notify.MAX_PER_RUN]})
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
