# クロール対象サイト調査

調査日: 2026-08-22

「車種 × 年式」ごとに (1) オークション相場 (2) 小売相場 (3) 口コミ (4) 整備観点の弱点
を毎週スナップショットするために、どのサイトから何をどう取るかを実測して決めたもの。
各サイトは実際に HTTP で叩いて構造とレスポンスを確認済み。

---

## 採用したソース一覧

| # | ソース | 取れるもの | 取得方式 | robots 上の扱い |
|---|--------|-----------|---------|----------------|
| 1 | ヤフオク! 落札相場 | **年式別の落札価格**、走行距離、入札数、落札日 | `__NEXT_DATA__` JSON | `Allow: /closedsearch/closedsearch` |
| 2 | カーセンサー 相場表 | 小売価格レンジ、**年式×価格帯クロス集計**、走行距離×価格帯、掲載台数、店舗数、口コミ6軸スコア | HTML パース | 該当 Disallow なし |
| 3 | 価格.com 自動車 | 新車価格、満足度、レビュー件数、評価軸別スコア | HTML パース | 該当 Disallow なし |
| 4 | みんカラ | オーナー口コミ、評価分布、整備・不具合の記述 | HTML パース | 該当 Disallow なし |
| 5 | 国交省 リコール届出情報 | **型式別リコール**: 不具合装置・状況説明・改善措置・対象台数 | JSON API | robots.txt なし |
| 6 | 国交省 不具合情報ホットライン | **ユーザー通報の不具合**: 装置・型式・初度登録年月・走行距離・症状 | JSON API | robots.txt なし |

補助的に グーネット (goo-net) のカタログを型式・世代の裏取りに使う。

---

## 1. ヤフオク! 落札相場 — オークション相場の主軸

### robots.txt の読み

`https://auctions.yahoo.co.jp/robots.txt` には

```
Disallow: /closedsearch/
...
Allow: /closedsearch/closedsearch
```

があり、最長一致ルールにより **`/closedsearch/closedsearch` は許可**されている。
ただし同ファイルで以下のクエリパラメータ付き URL は個別に禁止されているので、
リクエストに含めてはいけない:

`istatus` / `abatch` / `aucminprice` / `aucmaxprice` / `jpypayment` /
`pstagefree` / `offer` / `thumb` / `select` / `n` / `wheel_spec_id`

`n`（1ページ件数）が禁止なので**ページサイズは既定の 50 のまま**とし、`b`（オフセット）で
ページングする。

### カテゴリ構造

車両本体は `自動車、オートバイ(26318) > 中古車・新車(26360) > メーカー > 車種` の
リーフカテゴリに入る。パーツ類と混ざらないよう、**必ず車種リーフの `auccat` を指定**する。

```
26318  自動車、オートバイ
└ 26360  中古車・新車
   └ 2084007642  トヨタ
      ├ 2084049538  アルファード
      ├ 2084231229  アルファードハイブリッド
      ├ 2084231724  ヴェルファイア
      ├ 2084299521  ヴェルファイアハイブリッド
      ├ 2084049058  ヴォクシー
      ├ 2084016682  ノア
      ├ 2084315387  エスクァイア
      ├ 2084315388  エスクァイア ハイブリッド
      ├ 2084054059  シエンタ
      ├ 2084016637  エスティマ
      ├ 2084231230  エスティマハイブリッド
      ├ 2084299524  プリウスα
      ├ 2084051733  ウィッシュ
      └ 2084058459  アイシス
```

カテゴリ ID は `modules.category.children` から自動採取できるので、
`config/vehicles.toyota_minivan.yaml` に持ちつつ `scripts/scan_yahoo_categories.py` で更新可能。

### レスポンス

`<script id="__NEXT_DATA__">` の中に検索結果がそのまま入っている。

- `props.pageProps.initialState.search.items.listing.metadata.statistics`
  → `{avgPrice, maxPrice, minPrice}` = **180日間の落札相場サマリ**
- `...listing.items[]` → 1件ずつの落札データ

```jsonc
{
  "auctionId": "...",
  "title": "アルファード 2.5 Z トヨタセーフティセンス レザーシート",
  "price": 5080000,          // 落札価格
  "buyNowPrice": 5080000,
  "bidCount": 1,
  "endTime": "2026-08-21T17:07:22+09:00",
  "carSpec": {
    "mileage": 22000,             // 走行距離 km
    "mileageType": "REAL_MILEAGE" // or METER_REPLACEMENT (メーター交換)
    "modelDate": 20240401,        // ★ 年式 (YYYYMMDD)
    "repairType": "NONE",         // 修復歴
    "overheadCosts": 0            // 諸費用
  }
}
```

`carSpec.modelDate` があるので **年式別の落札相場を item レベルで直接集計できる**。
これが今回いちばん効いたポイント。

対象は「終了180日間」。毎週スナップショットを取ると窓が重なるが、
`auctionId` を持っておけば後から重複排除も差分抽出もできる。

### 集計方針

年式ごとに `n / min / p25 / median / mean / p75 / max` を出す。
`mileageType != REAL_MILEAGE`（メーター交換車）と `repairType != NONE`（修復歴あり）は
フラグを残しつつ、既定の集計からは除外して「実走・無修復ベース」を主系列にする。

---

## 2. カーセンサー 相場表 — 小売相場の主軸

`https://www.carsensor.net/usedcar/souba/<CODE>/` （例 `TO_S001` = アイシス）。
コード一覧は `https://www.carsensor.net/souba-shashu.xml` にある（全 2237 車種）。
コード→車種名の対応は `scripts/scan_carsensor_codes.py` で作って
`config/carsensor_codes.json` にコミットしてある。

robots.txt に `/usedcar/souba/` を禁じる行はない（禁止は `/usedcar/search.php` や
問い合わせ系のみ）。

1ページから取れるもの:

- 中古車価格レンジ（例 `13～195 万円`）
- 総合評価 / クチコミ件数 / **デザイン・走行性・居住性・運転しやすさ・積載性・維持費** の6軸
- ボディタイプ内ランキング順位
- 中古車掲載台数 / 取扱店舗数
- **「中古車情報の相場」= 年式 × 価格帯 のクロス集計表**
  （行 = `20万円未満`〜`420万円以上` の価格ビン、列 = `2012年以前`〜`2026年以降`）
- 走行距離 × 価格帯 のクロス集計表

年式×価格帯の度数分布があるので、**年式別に価格の中央値・平均をビン補間で推定**できる。
オークション相場（実売）と小売相場（売り値）の乖離＝業販マージンの目安にもなる。

---

## 3. 価格.com 自動車

`https://kakaku.com/kuruma/maker/toyota/` にトヨタの全車種（143件）が
`https://kakaku.com/item/<ID>/` 形式で並ぶ。主要ミニバンの ID は採取済み:

| 車種 | 価格.com item ID |
|---|---|
| アルファード | 70100110661 |
| ヴェルファイア | 70100110662 |
| ノア | 70100110045 |
| ヴォクシー | 70100110019 |
| シエンタ | 70100110391 |
| エスティマ | 70100110021 |
| WISH | 70100110323 |
| アイシス | 70100110472 |
| アルファードV | 70100110013 |

エンコーディングは Shift_JIS。新車価格帯・満足度・レビュー件数・評価軸別スコアを取る。
`robots.txt` の `Disallow` は `/ksearch/`, `/auth/`, `/kuchikomi/review/history/` 等で、
`/kuruma/` `/item/` は対象外。

---

## 4. みんカラ

`https://minkara.carview.co.jp/car/toyota/<slug>/` が車種の口コミ・評価トップ、
`.../review/` がレビュー一覧。robots.txt の禁止は `/include_api/` と編集系のみ。
オーナーの生の声と、整備手帳由来の不具合記述を拾う。

---

## 5-6. 国土交通省 — 「壊れやすい点」の一次ソース

旧 `carinf.mlit.go.jp` は廃止され、現在は
`https://renrakuda.mlit.go.jp/renrakuda/`（自動車不具合情報ホットライン「連ラクダ」）。
画面は SPA で、裏側は MovableType の Estraier 検索 CGI を叩いている。

```
GET https://renrakuda.mlit.go.jp/mt/mt-estraier.cgi
```

robots.txt は存在しない（404）。公的な公表情報であり、画面から CSV ダウンロードも
提供されている情報と同じもの。

### 5. リコール届出情報 (`class=recalldatacar`)

```
blog_id=4
class=recalldatacar
car_name_code=トヨタ            # 車名（メーカー名の文字列そのもの）
model_name=DBA-AGH30W           # ★ 型式（排ガス記号込みで完全一致）
notification_date=1990/01/01 2026/12/31
offset=1&limit=50
order_by=recall_data_car_mlit_notification_date&order_condition=STRD
```

返るもの（抜粋）:

```jsonc
{
  "recall_data_car_mlit_notification_date": "2025/01/22",
  "recall_data_car_mlit_defective_device": "燃料ポンプ",
  "recall_data_car_mlit_recall_car_count": "54577",
  "recall_data_car_mlit_situation_explanatory_text": "低圧燃料ポンプのインペラ…",
  "recall_data_car_mlit_measures_explanatory_text": "全車両、低圧燃料ポンプを対策品と交換する。",
  "typeList": [{ "recall_type_data_car_mlit_common_name": "アルファード", ... }]
}
```

**型式でクエリする**ので、車種マスタに世代ごとの型式を持たせておく必要がある。

### 6. 不具合情報 (`class=releasedatacar`)

ユーザーが国交省に通報した不具合。整備観点の弱点はこれがいちばん実態に近い。

```
blog_id=4
class=releasedatacar
release_data_car_mlit_reception_date=0000-00-00 9999-12-31
release_data_car_mlit_car_name=トヨタ
release_data_car_mlit_car_common_name=アルファード      # 通称名
offset=1&limit=50
order_by=release_data_car_mlit_reception_date&order_condition=STRD
```

返るもの:

```jsonc
{
  "release_data_car_mlit_reception_date": "2026-06-19",
  "release_data_car_mlit_defective_device": "保安灯火",
  "release_data_car_mlit_model": "3BA-AGH30W",          // 型式
  "release_data_car_mlit_initial_registration_date": "2023/01",  // ★ 初度登録 = 年式
  "release_data_car_mlit_total_mileage": "45000",
  "release_data_car_mlit_report_content_summary":
    "ヘッドライトが点灯した際にしばらくするとメーター内にチェックランプが点灯する。…",
  "count": "521"    // 該当総件数
}
```

`initial_registration_date` があるので **年式別に「どの装置が壊れているか」を集計できる**。
装置カテゴリ（`defective_device`）でランキングし、代表事例を数件添える。

---

## クロールのお作法

`src/kakaku_ai/http.py` に集約:

- ホストごとに最低待機時間（既定 2.0 秒、国交省 API は 1.5 秒）を空ける
- 素性のわかる User-Agent（連絡先 URL 入り）
- 429 / 5xx は指数バックオフで最大3回リトライ
- 取得済みレスポンスは `data/cache/` に日付キーでキャッシュし、同一週の再実行で叩き直さない
- 取得は各ソースの公開ページのみ。ログインが要る領域・禁止パラメータには触れない

## 取らないと判断したもの

- **業者オークション（USS / TAA / JU など）の落札相場** — 会員限定の有料情報で、
  一般公開されていない。オークネット等の公開部分も規約上の再配布が難しい。
  よって「オークション相場」はヤフオク!（一般公開）を主軸にする。
- **ヤフオク! の `/closedsearch/` 配下でパラメータ禁止に触れる呼び方** — robots で明示的に
  禁止されているため使わない。
- **オークファン等の集計サイト** — 一次情報ではなく、規約でスクレイピングを禁じている。
