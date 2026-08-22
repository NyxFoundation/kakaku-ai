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


@dataclass(frozen=True)
class Vehicle:
    key: str
    name: str
    name_en: str
    yahoo_categories: tuple[int, ...]
    carsensor_codes: tuple[str, ...]
    kakaku_item_id: str | None
    minkara_slug: str | None
    mlit_common_names: tuple[str, ...]
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

    def by_key(self, key: str) -> Vehicle:
        for v in self.vehicles:
            if v.key == key:
                return v
        raise KeyError(key)


def load_vehicles(path: Path | None = None) -> VehicleSet:
    path = path or CONFIG_DIR / "vehicles.toyota_minivan.yaml"
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    meta = raw["meta"]

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
        vehicles.append(
            Vehicle(
                key=item["key"],
                name=item["name"],
                name_en=item["name_en"],
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
                mlit_common_names=tuple(item.get("mlit_common_name") or (item["name"],)),
                generations=gens,
            )
        )

    return VehicleSet(
        maker=meta["maker"],
        maker_en=meta["maker_en"],
        body_type=meta["body_type"],
        model_year_from=int(meta["model_year_from"]),
        vehicles=tuple(vehicles),
    )
