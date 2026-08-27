"""カタログの車種を、みんカラ・国交省の呼び名に結びつける。

深掘り 20 車種は `config/vehicles.*.yaml` に手で書いてある。同じことを
2,237 車種でやるのは無理なので、こちらは**機械的に解決**する。

### みんカラ

`sources/minkara_index` が作る対応表（車種トップの `<title>` から取った
日本語名 → slug）に、カーセンサーの「メーカー + 車種名」で当てる。
表記が揺れるので、記号と空白を落として全角英数を半角に寄せてから比べる。

### 国交省

通称名で引くのだが、**表記が独特**で 2 つの壁がある。

* **メーカー名が違う。** 日産 →「ニッサン」。実測で「日産」「日産自動車」は
  どちらも 0 件だった
* **英数字が全角。** 「86」→「８６」、「RX-8」→「ＲＸ－８」、「S2000」→「Ｓ２０００」。
  半角のままだと 0 件になる

そこで候補を順に投げて、最初にヒットしたものを採用して控える。
上位 22 車種で試したところ 20 件が当たった。外れたのは
「ミニ ミニコンバーチブル」「スバル インプレッサハッチバック」で、
どちらも国交省側の通称名の切り方が違うとみている。

解決結果は `config/model_links.json` に貯める。毎回 API を叩き直さないため。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

from .http import Fetcher
from .sources import mlit
from .sources.minkara_index import load_index
from .vehicles import CONFIG_DIR

log = logging.getLogger(__name__)

LINKS_PATH = CONFIG_DIR / "model_links.json"

# 国交省でのメーカー表記。ここに無いものはカーセンサーの表記をそのまま使う
MLIT_MAKER = {
    "日産": "ニッサン",
    "ＢＭＷ": "ＢＭＷ",
    "ＭＩＮＩ": "ＭＩＮＩ",
}

_HALF = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-"
_FULL = "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ－"
_TO_FULL = str.maketrans(_HALF, _FULL)


def to_fullwidth(text: str) -> str:
    """英数字とハイフンを全角にする。国交省の通称名がこの表記のため。"""
    return text.translate(_TO_FULL)


def normalize(text: str | None) -> str:
    """突き合わせ用に寄せる。全角英数を半角に、記号と空白を落とす。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[\s・･\-－ー_（）()【】\[\]/／]", "", text).lower()


# --------------------------------------------------------------- 保存


def load_links() -> dict[str, dict[str, Any]]:
    if not LINKS_PATH.exists():
        return {}
    return json.loads(LINKS_PATH.read_text(encoding="utf-8"))


def save_links(links: dict[str, dict[str, Any]]) -> None:
    LINKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LINKS_PATH.write_text(
        json.dumps(links, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------- みんカラ


def resolve_minkara(maker: str | None, model_name: str | None) -> dict[str, Any] | None:
    """カタログの (メーカー, 車種) をみんカラの slug に当てる。ネットワーク不要。"""
    if not maker or not model_name:
        return None
    index = load_index()
    want = normalize(f"{maker}{model_name}")
    want_model = normalize(model_name)

    exact = None
    same_model = None
    for entry in index.values():
        key = normalize(f"{entry['maker_ja']}{entry['name_ja']}")
        if key == want:
            exact = entry
            break
        # メーカー表記が違う場合に備えて、車種名だけの一致も控えておく
        if same_model is None and normalize(entry["name_ja"]) == want_model:
            same_model = entry
    hit = exact or same_model
    if not hit:
        return None
    return {"maker_slug": hit["maker_slug"], "slug": hit["slug"],
            "matched": hit["name_ja"], "exact": bool(exact)}


# --------------------------------------------------------------- 国交省


def _has_defects(fetcher: Fetcher, maker: str, common_name: str) -> bool:
    for _ in mlit._paginate(
        fetcher,
        {
            "class": "releasedatacar",
            "release_data_car_mlit_reception_date": "0000-00-00 9999-12-31",
            "release_data_car_mlit_car_name": maker,
            "release_data_car_mlit_car_common_name": common_name,
        },
        "release_data_car_mlit_reception_date",
    ):
        return True
    return False


def resolve_mlit(fetcher: Fetcher, maker: str | None,
                 model_name: str | None) -> dict[str, Any] | None:
    """国交省で実際にヒットする (メーカー, 通称名) を探す。当たるまで候補を投げる。"""
    if not maker or not model_name:
        return None
    mlit_maker = MLIT_MAKER.get(maker, maker)
    candidates = [model_name, to_fullwidth(model_name)]
    # 「インプレッサハッチバック」のように後ろが付いている車種は、
    # 素の車名でも引いてみる（国交省側は切り方が粗いことがある）
    base = re.sub(r"(ハッチバック|クーペ|セダン|ワゴン|コンバーチブル|カブリオレ"
                  r"|スポーツ|クロスオーバー)$", "", model_name)
    if base and base != model_name:
        candidates += [base, to_fullwidth(base)]

    seen: set[str] = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        try:
            if _has_defects(fetcher, mlit_maker, name):
                return {"maker": mlit_maker, "common_name": name,
                        "exact": name == model_name}
        except Exception as exc:  # noqa: BLE001 - 1件のこけで止めない
            log.warning("  mlit %s %s: %s", mlit_maker, name, exc)
            return None
    return None


# --------------------------------------------------------------- まとめ


def build(fetcher: Fetcher, summaries: list[dict[str, Any]], *,
          refresh: bool = False) -> dict[str, dict[str, Any]]:
    """カタログの車種ぶんの対応を解決して貯める。

    みんカラはローカルの対応表を引くだけなので毎回やり直す。
    国交省は 1 車種につき最大 4 リクエスト叩くので、一度解決したら触らない。
    """
    links = {} if refresh else load_links()

    for i, summary in enumerate(summaries, 1):
        code = summary["carsensor_code"]
        maker, model_name = summary.get("maker"), summary.get("model_name")
        entry = links.setdefault(code, {"maker": maker, "model_name": model_name})

        # みんカラはネットワーク不要なので毎回引き直す（対応表が育つため）
        entry["minkara"] = resolve_minkara(maker, model_name)

        if "mlit" not in entry:
            entry["mlit"] = resolve_mlit(fetcher, maker, model_name)
            if i % 50 == 0:
                resolved = sum(1 for v in links.values() if v.get("mlit"))
                log.info("  %s/%s 解決（国交省ヒット %s件）", i, len(summaries), resolved)
                save_links(links)

    save_links(links)
    minkara_n = sum(1 for v in links.values() if v.get("minkara"))
    mlit_n = sum(1 for v in links.values() if v.get("mlit"))
    log.info("対応づけ完了: %s車種 / みんカラ %s件 / 国交省 %s件",
             len(links), minkara_n, mlit_n)
    return links
