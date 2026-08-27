"""コマンドライン。

    uv run kakaku-ai crawl                 # 今日のスナップショットを取る
    uv run kakaku-ai crawl --only alphard  # 車種を絞る
    uv run kakaku-ai excel                 # 全スナップショットから xlsx を作る
    uv run kakaku-ai upload                # Drive に上げる
    uv run kakaku-ai weekly                # crawl → excel → upload を通しで
    uv run kakaku-ai classics              # 旧車（1988〜2001年式）の在庫を全メーカー集める
    uv run kakaku-ai books                 # xlsx 4冊を作り直して Drive に上げる
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import classics, drive, excel, notify, pipeline, store, watch, wide
from .vehicles import DATA_DIR, load_vehicles

OUTPUT_DIR = DATA_DIR / "xlsx"
OUTPUT_NAME = "souba_minivan.xlsx"


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
    wa.add_argument("--include-pricey", action="store_true", help="相場より高いものも流す（既定は安いものだけ）")
    wa.add_argument("--year-from", type=int, help="対象年式の下限（既定=車種マスタの設定）")
    wa.add_argument("--no-detail", action="store_true", help="商品ページを開かない（写真枚数と説明文の記載を省く）")
    wa.add_argument(
        "--repair",
        choices=watch.REPAIR_MODES,
        default="none",
        help="修復歴の絞り方。none=申告が「なし」のものだけ（既定） / any=絞らない",
    )

    wd = sub.add_parser("wide", help="全車種・全年式の小売相場を集める（カーセンサー）")
    wd.add_argument("--snapshot", help="YYYY-MM-DD（既定=今日）")
    wd.add_argument("--prefix", help="メーカー接頭辞で絞る（例 TO）")
    wd.add_argument("--limit", type=int, help="車種数の上限（試運転用）")
    wd.add_argument("--year-from", type=int, help="年式の下限（既定=全年式）")
    wd.add_argument("--no-cache", action="store_true")

    cs = sub.add_parser("classics", help="旧車の在庫を全メーカー集めて xlsx にする")
    cs.add_argument("--year-from", type=int, default=classics.YEAR_FROM)
    cs.add_argument("--year-to", type=int, default=classics.YEAR_TO)
    cs.add_argument("--maker", nargs="*", help="メーカー名で絞る（既定=全部）")
    cs.add_argument("--limit", type=int, help="車種数の上限（試運転用）")
    cs.add_argument("--max-pages", type=int, default=5, help="1車種あたりのページ数")
    cs.add_argument("--snapshot", help="YYYY-MM-DD（既定=今日）")
    cs.add_argument("-o", "--output")
    cs.add_argument("--folder-id", default=drive.DEFAULT_FOLDER_ID)
    cs.add_argument("--upload", action="store_true", help="作った xlsx を Drive に上げる")
    cs.add_argument("--no-cache", action="store_true")
    cs.add_argument("--rebuild", action="store_true",
                    help="取得はせず、保存済みの在庫から xlsx を作り直す")
    cs.add_argument("--yahoo", action="store_true",
                    help="ヤフオクの落札（過去180日）も取る。中古車ノードを"
                         "並び順を変えて全部さらうので 40〜50分かかる")

    bk = sub.add_parser("books", help="xlsx 4冊（全車種・ミニバン・普通車・旧車）を組む")
    bk.add_argument("--only", nargs="*",
                    choices=["all", "minivan", "standard", "classics", "sportscar"],
                    help="作る本を絞る（既定=全部）")
    bk.add_argument("--folder-id", default=drive.DEFAULT_FOLDER_ID)
    bk.add_argument("--no-upload", action="store_true")

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

    if args.command == "wide":
        from .http import Fetcher
        from .pipeline import CACHE_DIR

        snapshot = args.snapshot or store.today()
        fetcher = Fetcher(cache_dir=CACHE_DIR, snapshot=snapshot, use_cache=not args.no_cache)
        summaries, by_year = wide.crawl(
            fetcher, snapshot, prefix=args.prefix, limit=args.limit,
            model_year_from=args.year_from,
        )
        # 部分実行でも既存を消さないよう、pipeline と同じ扱いにする
        for dataset, rows in (("wide_summary", summaries), ("wide_by_year", by_year)):
            merged = pipeline._merge_with_existing(snapshot, dataset, rows, None)
            store.write(snapshot, dataset, merged)
        print(f"車種 {len(summaries)} / 年式別相場 {len(by_year)}行 を {snapshot} に保存")
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
            cheap_only=not args.include_pricey,
        )

        seen = {} if args.all else notify.load_seen()
        # ローカルの既読が失われていても二度流さないよう、Slack の履歴も突き合わせる
        already = set(seen)
        if not args.all:
            from_channel = notify.posted_in_channel(args.channel)
            new_to_file = from_channel - already
            if new_to_file:
                logging.getLogger(__name__).info(
                    "  Slack 履歴から既読 %s件を回収（ローカルに無かった分）", len(new_to_file)
                )
                for auction_id in new_to_file:
                    seen[auction_id] = ""
            already |= from_channel

        fresh = [r for r in picked if r["auction_id"] not in already]

        # 通知する分だけ商品ページを開いて、写真の枚数と説明文の記載を足す。
        # 一覧には入っていない情報で、拾うと注意点がだいぶ具体的になる。
        # 全件やると重いので、実際に流す上限のぶんだけ。
        head = fresh[: notify.MAX_PER_RUN]
        if head and not args.no_detail:
            from .sources import yahoo_detail

            yahoo_detail.enrich(Fetcher(use_cache=False), head)
            # 価格の判定はやり直さない。即決の基準が少数から再計算されて狂う。
            watch.refresh_risk(head, defects)
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

        conditions = [f"予算 {args.budget:.0f}万円" if args.budget else "予算指定なし"]
        conditions.append("修復歴なしのみ" if args.repair == "none" else "修復歴で絞らない")
        conditions.append("安い側のみ" if not args.include_pricey else "高い側も含む")
        if args.individual_only:
            conditions.append("個人出品のみ")
        conditions.append(f"{args.year_from or vehicles.model_year_from}年式以降")

        notify.post(
            fresh,
            channel=args.channel,
            dry_run=args.dry_run,
            header=f"気になる出品 {min(len(fresh), notify.MAX_PER_RUN)}件",
            subtitle=(
                f"出品 {len(listings)}件を確認 → 該当 {len(picked)}件 → 新着 {len(fresh)}件"
                f"　|　{' ・ '.join(conditions)}"
            ),
        )
        if not args.dry_run:
            # 実際に流したものだけを既読にする。スキャンした全件を既読にすると、
            # 今日はまだ競り上がり前で判定保留だった出品が、終了間際になっても
            # 二度と通知されなくなる。終了日時も残して、終わったものは後で整理する。
            for r in fresh[: notify.MAX_PER_RUN]:
                seen[r["auction_id"]] = r.get("end_time") or ""
            notify.save_seen(seen)
        return 0

    if args.command == "classics":
        from . import classics_excel
        from .http import Fetcher
        from .pipeline import CACHE_DIR
        from .sources import carsensor_listings

        snapshot = args.snapshot or store.today()
        if args.rebuild:
            listings, found_on = store.read_latest("classic_listings")
            if not listings:
                raise SystemExit("保存済みの旧車在庫がありません。--rebuild なしで実行してください。")
            snapshot = args.snapshot or found_on or snapshot
            listings = classics.reapply_catalog(listings)
            store.write(snapshot, "classic_listings", listings)
        else:
            fetcher = Fetcher(cache_dir=CACHE_DIR, snapshot=snapshot, use_cache=not args.no_cache)
            listings = classics.crawl(
                fetcher,
                year_from=args.year_from,
                year_to=args.year_to,
                makers=args.maker,
                limit=args.limit,
                max_pages=args.max_pages,
            )
            # 在庫ストアにも積んでおく。週を空けて撮り直すと値下げが履歴に残る。
            # 掲載終了の判定は track() 側の役目なのでここではしない
            carsensor_listings.record(listings, snapshot)
            store.write(snapshot, "classic_listings", listings)

        if args.yahoo:
            from .sources import yahoo_used_cars

            fetcher = Fetcher(cache_dir=CACHE_DIR, snapshot=snapshot,
                              use_cache=not args.no_cache)
            auctions = yahoo_used_cars.sweep(fetcher, snapshot, model_year_to=args.year_to)
            store.write(snapshot, "classic_auctions", auctions)
        else:
            # 取り直さないときは保存済みを使う。落札はもう変わらないので
            # 毎回 50 分かけて取り直す意味がない
            auctions, _ = store.read_latest("classic_auctions")

        models = classics.by_model(listings)
        path = Path(args.output) if args.output else classics_excel.DEFAULT_OUTPUT
        classics_excel.build(listings, models, auctions=auctions, output=path,
                             year_from=args.year_from, year_to=args.year_to, snapshot=snapshot)
        if args.upload:
            drive.upload(path, folder_id=args.folder_id, snapshot=snapshot)
        print(f"旧車 在庫{len(listings)}台 / {len(models)}車種 / 落札{len(auctions)}台 → {path}")
        return 0

    if args.command == "books":
        from . import books, classics_excel, sportscar

        wanted = set(args.only or ["all", "minivan", "standard", "classics", "sportscar"])
        snapshot = store.list_snapshots()[-1] if store.list_snapshots() else None
        built: list[Path] = []

        if "all" in wanted:
            built.append(books.catalog_book(
                books.OUTPUT_DIR / "souba_all.xlsx",
                title="全車種カタログ（カーセンサー掲載の全 2,237 車種）",
                note="車種を決めていない人向けの索引。ボディタイプとメーカーで当たりを"
                     "付けて、決まったら用途別の本（ミニバン・普通車・旧車）に移る。",
            ))
        if "standard" in wanted:
            built.append(books.catalog_book(
                books.OUTPUT_DIR / "souba_standard.xlsx",
                title="普通車（乗用車）から選ぶ",
                note="ミニバンとトラックを除いた乗用車。ハッチバック・セダン・SUV・"
                     "クーペ・ワゴン・オープン。車種比較シートだけで候補を絞れるようにしてある。",
                body_types=books.STANDARD_BODIES,
            ))
        if "minivan" in wanted:
            built.append(excel.build(OUTPUT_DIR / OUTPUT_NAME))
        if "classics" in wanted:
            listings, snap = store.read_latest("classic_listings")
            auctions, _ = store.read_latest("classic_auctions")
            if listings:
                listings = classics.reapply_catalog(listings)
                built.append(classics_excel.build(
                    listings, classics.by_model(listings), auctions=auctions,
                    output=classics_excel.DEFAULT_OUTPUT,
                    year_from=classics.YEAR_FROM, year_to=classics.YEAR_TO,
                    snapshot=snap or snapshot or store.today()))
            else:
                logging.getLogger(__name__).warning(
                    "旧車データがありません。`kakaku-ai classics` を先に実行してください。")

        if "sportscar" in wanted:
            built.append(sportscar.build())

        if not args.no_upload:
            for path in built:
                drive.upload(path, folder_id=args.folder_id, snapshot=snapshot)
        print("作成:", ", ".join(p.name for p in built))
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
