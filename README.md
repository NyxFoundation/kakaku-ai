# kakaku-ai

中古車の**相場・口コミ・壊れやすい点**を毎週スナップショットして、
時系列の追える 1 冊の xlsx にまとめるパイプライン。

第一弾の対象は **2013年式以降のトヨタ・ミニバン 11 車種**
（アルファード / ヴェルファイア / ノア / ヴォクシー / エスクァイア / シエンタ /
エスティマ / ウィッシュ / プリウスα / アイシス / グランエース）。

出力物は Google Drive の共有フォルダにも週次で上がる。

---

## 何が入っているか

| シート | 中身 |
|---|---|
| `グラフ_価格差` | 落札価格と店頭価格の差が大きい順の横棒。走行距離も併記。**まずここ** |
| `グラフ_車種別` | 車種ごとの落札相場 vs 小売相場の棒グラフ |
| `グラフ_年式別` | 車種 × 年式 の落札中央値マトリクス（行内で色分け）＋ 値落ちカーブ |
| `相場_最新` | 直近時点の **車種 × 年式** 相場を数字で |
| `相場_時系列` | 全スナップショットを積んだ long format |
| `推移_落札中央値` / `推移_小売中央値` | 行=車種×年式 / 列=時点 のピボット。折れ線はここから |
| `車種サマリ` | 新車・中古価格レンジ、掲載台数、店舗数、満足度 |
| `口コミ_年式別` / `口コミ_明細` | みんカラのレビュー（年式・グレード・6軸スコア・満足/不満/総評） |
| `壊れやすい点` | 国交省の不具合通報を装置別に集計。発生時の走行距離中央値つき |
| `リコール` | 国交省リコール届出（不具合の状況・改善措置の全文） |
| `相場_累計` | 全スナップショットの落札を名寄せした年式集計。**サンプルが欲しいときはこちら** |
| `落札明細` | ヤフオク!の落札 1 台ずつ（累計）。グレード・車検・諸費用込み総額つき |
| `車種マスタ` | 車種・世代・型式の一覧 |

相場は 2 系統を並べて持つ:

- **オークション相場** — ヤフオク!「中古車・新車」の終了180日間の落札実績
- **小売相場** — カーセンサー掲載車の価格分布

この差（`小売プレミアム`）が、業販と店頭の値付けギャップの目安になる。

### 落札データは週を重ねるほど厚くなる

ヤフオクの落札検索は「終了180日間」しか返さない。ただし週次スナップショットを
`auction_id` で名寄せしているので、**実効期間は 180 日を超えて伸びていく**。
半年回せば約 1 年ぶんが貯まる計算で、年式ごとのサンプル数もそのぶん増える。

あわせて、落札商品ページ（`/jp/auction/<id>`）から検索結果には無い
グレード・車検・修復歴・諸費用込み総額を取っている。落札済みの出品はもう変わらないので、
パース結果は `data/auction_details.jsonl` に永続キャッシュして二度と取りに行かない。
初回だけ 780 ページぶん時間がかかるが、2 週目以降は新規分（週 30〜60 件）だけ。

### 相場を並べるときの落とし穴

2 系統をそのまま比べると数字が嘘をつくので、2 つ補正している。

1. **年式構成のズレ** — ヤフオクの落札は古い年式に、カーセンサーの掲載は新しい年式に偏る。
   車種単位で単純に中央値を比べると、ノアで差額 243万円という実在しない数字が出た。
   `グラフ_車種別` では小売側を**落札と同じ年式構成で重み付け**し直している（同じノアで 29万円）。
2. **走行距離のズレ** — 同じ年式で揃えても、落札車は距離が伸びている。
   シエンタ 2016年式は落札車が中央 22.0万km、掲載車が 3.9万km。
   価格差 159% はほぼこれで説明がつく。だから `グラフ_価格差` には両方の走行距離を並べてある。
   **差額をそのまま利ざやと読んではいけない。**

---

## 使い方

```bash
uv venv && uv pip install -e .

uv run kakaku-ai crawl                  # 今日のスナップショットを取る
uv run kakaku-ai crawl --only alphard   # 車種を絞る
uv run kakaku-ai crawl --sources yahoo  # ソースを絞る
uv run kakaku-ai excel                  # 全スナップショットから xlsx を組む
uv run kakaku-ai upload                 # Drive にアップロード
uv run kakaku-ai weekly                 # 上の 3 つを通しで
uv run kakaku-ai list                   # 収録済みスナップショットを表示
```

`crawl` は取得した生レスポンスを `data/cache/<日付>/` に置くので、
同じ日に何度回しても相手サイトを叩き直さない。
パーサだけ直して再集計したいときは `crawl` をもう一度走らせればキャッシュから作り直す。
本当に取り直したいときだけ `--no-cache`。

---

## 週次で回す

### ローカル（systemd user timer）— こちらが本番

毎週月曜 04:23 に `scripts/weekly.sh` が走り、
**crawl → xlsx 生成 → Drive アップロード → main へ push** までを通す。
rclone の認証情報がローカルにあるので、これがいちばん素直。

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd/kakaku-ai-weekly.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kakaku-ai-weekly.timer

systemctl --user list-timers kakaku-ai-weekly.timer   # 次回の実行予定
systemctl --user start kakaku-ai-weekly.service       # 今すぐ 1 回まわす
journalctl --user -u kakaku-ai-weekly -f              # ログ
systemctl --user disable --now kakaku-ai-weekly.timer # やめる
```

マシンが落ちていて実行時刻を逃しても `Persistent=true` で次の起動時に追いつく。

### GitHub Actions（バックアップ）

`.github/workflows/weekly.yml` が毎週日曜 19:17 UTC に
`crawl` → `excel` → コミット → push をやる。
Drive へのアップロードは rclone の認証情報が要るので、
`RCLONE_CONFIG_GDRIVE_TOKEN` / `_CLIENT_ID` / `_CLIENT_SECRET` が
Secrets に設定されているときだけ実行される（未設定ならスキップ）。

### 手で回す

```bash
uv run kakaku-ai weekly    # crawl → excel → upload
./scripts/weekly.sh        # 上に加えて git push まで
```

---

## 設計

- **過去のスナップショットは絶対に書き換えない。** 週次実行は
  `data/snapshots/<YYYY-MM-DD>/` を 1 つ足すだけ。xlsx は毎回全断面を読み直して組み直す。
- 相場は long format（1行 = 時点 × 車種 × 年式）。週を重ねても列は増えない。
- 車種マスタ `config/vehicles.toyota_minivan.yaml` が全ソースの結節点。
  ここに各サイトの ID を持たせて、収集側は ID を引くだけにしてある。
  他メーカー・他ボディタイプに広げるときは、この YAML を増やす。

詳しくは:

- [`docs/crawl-research.md`](docs/crawl-research.md) — どのサイトから何をどう取るか（実測ベース）
- [`docs/schema.md`](docs/schema.md) — データスキーマと相場の算出方法

---

## クロールのお作法

- `robots.txt` を読んだうえで、許可されている経路だけを使っている。
  ヤフオク!は `/closedsearch/closedsearch` が `Allow` されている一方、
  禁止クエリパラメータが列挙されているので、コード側でも
  `FORBIDDEN_PARAMS` として弾いて事故らないようにしてある。
- ホストごとに 1.5〜2.5 秒の間隔を空ける。ジッタも入れる。
- 素性のわかる User-Agent（このリポジトリの URL 入り）。
- 429 / 5xx は指数バックオフで最大 3 回。
- 業者オークション（USS / TAA など）の落札相場は会員限定の有料情報なので**扱わない**。

---

## データソース

| ソース | 用途 |
|---|---|
| [ヤフオク!](https://auctions.yahoo.co.jp/) | オークション落札相場（年式・走行距離つき） |
| [カーセンサー](https://www.carsensor.net/) | 小売相場（年式×価格帯の度数分布）、評価6軸 |
| [価格.com](https://kakaku.com/kuruma/) | 新車価格、満足度、レビュー件数 |
| [みんカラ](https://minkara.carview.co.jp/) | オーナー口コミ |
| [国土交通省 連ラクダ](https://renrakuda.mlit.go.jp/renrakuda/) | リコール届出・不具合情報 |
