"""Google Drive へのアップロード（rclone 経由）。

`rclone` の `gdrive:` リモートを使い、`--drive-root-folder-id` で共有フォルダを
直接ルートに据える。最新版を上書きしつつ、`history/` に日付つきのコピーも
残すので、あとから過去の xlsx をそのまま取り出せる。

### 宛先はファイル名ではなく **Drive のファイル ID** で覚える

Drive の共有 URL は `.../file/d/<ID>/view` で、**名前を含まない**。だから
Drive 上で改名しても URL は生き続ける（実測で確認済み）。

ところが `rclone copyto` の宛先は名前で書く。フォルダ側で改名されていると
「その名前のファイルが無い」と判断して**新しいファイルを作ってしまう**。
すると

* 改名されたほう … 共有 URL は生きているが二度と更新されない（中身が凍る）
* 新しく作られたほう … 以後こちらだけ更新される

という、いちばん気づきにくい壊れ方になる。リンクは開けるのに中身が古い。

なので `config/drive_files.json` に **ローカルのファイル名 → Drive のファイル ID**
を控えておき、アップロード時に ID から**今の名前**を引いてそこへ書く。
これで Drive 上でいつ改名されても追随する。ID が見つからないとき（消された、
まだ 1 度も上げていない）はローカル名で新規に置き、その ID を控える。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .vehicles import CONFIG_DIR

log = logging.getLogger(__name__)

DEFAULT_FOLDER_ID = "1vrtzKA8Epn5IIbM0tzjWeq_ntQIWDzKW"
REMOTE = "gdrive:"
PINS_PATH = CONFIG_DIR / "drive_files.json"


def _rclone(args: list[str], folder_id: str) -> subprocess.CompletedProcess[str]:
    if not shutil.which("rclone"):
        raise RuntimeError("rclone が見つかりません")
    cmd = ["rclone", "--drive-root-folder-id", folder_id, *args]
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


# --------------------------------------------------------------- ID の控え


def load_pins() -> dict[str, str]:
    """ローカルのファイル名 → Drive のファイル ID。"""
    if not PINS_PATH.exists():
        return {}
    return json.loads(PINS_PATH.read_text(encoding="utf-8"))


def save_pins(pins: dict[str, str]) -> None:
    PINS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PINS_PATH.write_text(
        json.dumps(pins, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _index(folder_id: str) -> list[dict[str, Any]]:
    """フォルダ直下の一覧（名前と ID）。"""
    return [e for e in json.loads(_rclone(["lsjson", REMOTE], folder_id).stdout)
            if not e.get("IsDir")]


def _resolve(entries: list[dict[str, Any]], pins: dict[str, str],
             local_name: str) -> tuple[str, bool]:
    """書き込む先の**今の名前**と、それが控えた ID 由来かどうかを返す。"""
    pinned = pins.get(local_name)
    if pinned:
        for entry in entries:
            if entry["ID"] == pinned:
                return entry["Name"], True
        log.warning("  控えていた ID %s が見つかりません（%s）。"
                    "消されたとみて %s として置き直します",
                    pinned, local_name, local_name)
    return local_name, False


# --------------------------------------------------------------- アップロード


def upload(
    path: Path,
    *,
    folder_id: str = DEFAULT_FOLDER_ID,
    snapshot: str | None = None,
    keep_history: bool = True,
) -> None:
    """xlsx を最新版として置き、履歴も残す。宛先は ID で追う（モジュール冒頭参照）。"""
    pins = load_pins()
    entries = _index(folder_id)
    remote_name, followed = _resolve(entries, pins, path.name)

    _rclone(["copyto", str(path), f"{REMOTE}{remote_name}"], folder_id)
    if followed and remote_name != path.name:
        log.info("アップロード完了: %s（Drive 上の名前 / ID で追跡）", remote_name)
    else:
        log.info("アップロード完了: %s", remote_name)

    # 初回、または置き直したときに ID を控える
    if not followed:
        for entry in _index(folder_id):
            if entry["Name"] == remote_name:
                pins[path.name] = entry["ID"]
                save_pins(pins)
                log.info("  ファイル ID を控えました: %s → %s", path.name, entry["ID"])
                break

    if keep_history and snapshot:
        # 履歴はローカル名で積む。Drive 側の改名に引きずられると
        # 過去ファイルの名前がバラバラになって並べ替えられなくなる
        historical = f"{path.stem}_{snapshot}{path.suffix}"
        _rclone(["copyto", str(path), f"{REMOTE}history/{historical}"], folder_id)
        log.info("履歴を保存: history/%s", historical)


def listing(folder_id: str = DEFAULT_FOLDER_ID) -> str:
    return _rclone(["lsl", REMOTE], folder_id).stdout
