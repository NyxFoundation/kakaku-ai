"""車種マスタの読み込みと、年式 → 世代 の解決。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"


def _ym_to_int(value: str | None) -> int | None:
    """'2015-01' -> 201501"""
    if not value:
        return None
    y, m = value.split("-")
    return int(y) * 100 + int(m)


@dataclass(frozen=True)
class Generation:
    code: str
    year_month_from: int | None
    year_month_to: int | None
    models: tuple[str, ...]

    @property
    def label(self) -> str:
        def fmt(v: int | None) -> str:
            if v is None:
                return ""
            return f"{v // 100}/{v % 100:02d}"

        return f"{self.code} ({fmt(self.year_month_from)}〜{fmt(self.year_month_to)})"

    def covers(self, year_month: int) -> bool:
        if self.year_month_from is not None and year_month < self.year_month_from:
            return False
        if self.year_month_to is not None and year_month >= self.year_month_to:
            return False
        return True


# 国交省の「車名」表記はメーカーの一般表記とずれる（日産 → ニッサン）
MLIT_MAKER = {"日産": "ニッサン"}
# みんカラ / ジモティー の URL に使うメーカー識別子
MINKARA_MAKER = {
    "トヨタ": "toyota", "ホンダ": "honda", "日産": "nissan",
    "三菱": "mitsubishi", "マツダ": "mazda",
}
JMTY_MAKER = {
    "トヨタ": "toy", "ホンダ": "hon", "日産": "nis", "三菱": "mit", "マツダ": "maz",
}


@dataclass(frozen=True)
class Vehicle:
    key: str
    name: str
    name_en: str
    maker: str
    yahoo_categories: tuple[int, ...]
    carsensor_codes: tuple[str, ...]
    kakaku_item_id: str | None
    minkara_slug: str | None
    jmty_category: str | None
    jmty_keyword: str | None
    jmty_title_pattern: str | None
    jmty_maker: str
    minkara_maker: str
    mlit_maker: str
    mlit_common_names: tuple[str, ...]
    # 車種ごとの年式下限。世代交代や初期ロットの不具合で「この年式より前は
    # 買わない」が車種ごとに違うため、セット全体の下限とは別に持つ
    model_year_from: int | None = None
    generations: tuple[Generation, ...] = field(default=())

    def generation_for(self, year_month: int | None) -> Generation | None:
        if year_month is None:
            return None
        for gen in self.generations:
            if gen.covers(year_month):
                return gen
        return None

    def generation_label(self, year_month: int | None) -> str:
        gen = self.generation_for(year_month)
        return gen.code if gen else ""

    def generation_for_model_year(self, year: int | None) -> str:
        """年式（月が分からない）から世代を決める。

        「6月とみなして判定」だと年の途中でモデルチェンジした年式が落ちる。
        シエンタ 2015 は 170系が 7月 開始なので 6月判定では世代なしになり、
        落札 12 件・掲載 378 件が世代不明のまま出ていた。

        そこで、その暦年 12 か月のうち各世代が何か月ぶんを覆うかを数え、
        いちばん長い世代を採る。年をまたいで拮抗しているとき（アルファード 2023 の
        30系後期 5か月 / 40系 7か月 など）は、どちらか一方に決めると嘘になるので
        `30系後期/40系` のように併記する。
        """
        if not year:
            return ""

        overlap: list[tuple[Generation, int]] = []
        for gen in self.generations:
            months = sum(1 for m in range(1, 13) if gen.covers(year * 100 + m))
            if months:
                overlap.append((gen, months))
        if not overlap:
            return ""

        overlap.sort(key=lambda x: -x[1])
        total = sum(m for _, m in overlap)
        if overlap[0][1] / total >= 0.8:
            return overlap[0][0].code
        # 世代交代の年。定義順（古い順）に並べ直して併記する
        codes = [g.code for g in self.generations if any(g is o for o, _ in overlap)]
        return "/".join(codes)

    @property
    def all_models(self) -> list[str]:
        seen: list[str] = []
        for gen in self.generations:
            for m in gen.models:
                if m not in seen:
                    seen.append(m)
        return seen


@dataclass(frozen=True)
class VehicleSet:
    maker: str
    maker_en: str
    body_type: str
    model_year_from: int
    vehicles: tuple[Vehicle, ...]

    def __iter__(self):
        return iter(self.vehicles)

    def __len__(self) -> int:
        return len(self.vehicles)

    @property
    def mlit_makers(self) -> list[str]:
        """国交省のリコール検索に投げるメーカー名（重複なし）。"""
        out: list[str] = []
        for v in self.vehicles:
            if v.mlit_maker not in out:
                out.append(v.mlit_maker)
        return out

    def by_key(self, key: str) -> Vehicle:
        for v in self.vehicles:
            if v.key == key:
                return v
        raise KeyError(key)


def load_vehicles(paths: Path | list[Path] | None = None) -> VehicleSet:
    """車種マスタを読む。複数ファイルを渡すと連結する。

    メーカーをまたぐので、`meta.maker` は既定値でしかなく、
    実際に使うのは車種ごとの `maker`。
    """
    if paths is None:
        paths = sorted(CONFIG_DIR.glob("vehicles.*.yaml"))
    elif isinstance(paths, Path):
        paths = [paths]

    vehicles: list[Vehicle] = []
    meta: dict[str, Any] = {}
    for path in paths:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        meta = meta or raw["meta"]
        vehicles.extend(_parse_vehicles(raw, raw["meta"]))

    seen: set[str] = set()
    unique: list[Vehicle] = []
    for v in vehicles:
        if v.key not in seen:
            seen.add(v.key)
            unique.append(v)

    return VehicleSet(
        maker=meta["maker"],
        maker_en=meta["maker_en"],
        body_type=meta["body_type"],
        model_year_from=int(meta["model_year_from"]),
        vehicles=tuple(unique),
    )


def _parse_vehicles(raw: dict[str, Any], meta: dict[str, Any]) -> list[Vehicle]:

    # カーセンサーのコードは scripts/scan_carsensor_codes.py が作った表から車種名で引く。
    # YAML 側に明示指定があればそちらを優先する。
    cs_codes: dict[str, str] = {}
    cs_path = CONFIG_DIR / "carsensor_codes.json"
    if cs_path.exists():
        for code, name in json.loads(cs_path.read_text(encoding="utf-8")).items():
            cs_codes.setdefault(name, code)

    vehicles: list[Vehicle] = []
    for item in raw["vehicles"]:
        gens = tuple(
            Generation(
                code=g["code"],
                year_month_from=_ym_to_int(g.get("from")),
                year_month_to=_ym_to_int(g.get("to")),
                models=tuple(g.get("models") or ()),
            )
            for g in item.get("generations") or ()
        )
        maker = item.get("maker") or meta["maker"]
        vehicles.append(
            Vehicle(
                key=item["key"],
                name=item["name"],
                name_en=item["name_en"],
                maker=maker,
                yahoo_categories=tuple(item.get("yahoo_category") or ()),
                carsensor_codes=tuple(
                    code
                    for code in (
                        item.get("carsensor_code")
                        or [cs_codes.get(n) for n in (item.get("carsensor_names") or [item["name"]])]
                    )
                    if code
                ),
                kakaku_item_id=item.get("kakaku_item_id"),
                minkara_slug=item.get("minkara_slug"),
                jmty_category=item.get("jmty_category"),
                jmty_keyword=item.get("jmty_keyword"),
                jmty_title_pattern=item.get("jmty_title_pattern"),
                jmty_maker=item.get("jmty_maker") or JMTY_MAKER.get(maker, "toy"),
                minkara_maker=item.get("minkara_maker") or MINKARA_MAKER.get(maker, "toyota"),
                mlit_maker=item.get("mlit_maker") or MLIT_MAKER.get(maker, maker),
                mlit_common_names=tuple(item.get("mlit_common_name") or (item["name"],)),
                model_year_from=(int(item["model_year_from"])
                                 if item.get("model_year_from") else None),
                generations=gens,
            )
        )

    return vehicles
