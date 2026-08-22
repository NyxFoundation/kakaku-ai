"""Google Drive へのアップロード（rclone 経由）。

`rclone` の `gdrive:` リモートを使い、`--drive-root-folder-id` で共有フォルダを
直接ルートに据える。最新版を固定名で上書きしつつ、`history/` に日付つきの
コピーも残すので、あとから過去の xlsx をそのまま取り出せる。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_FOLDER_ID = "1vrtzKA8Epn5IIbM0tzjWeq_ntQIWDzKW"
REMOTE = "gdrive:"


def _rclone(args: list[str], folder_id: str) -> subprocess.CompletedProcess[str]:
    if not shutil.which("rclone"):
        raise RuntimeError("rclone が見つかりません")
    cmd = ["rclone", "--drive-root-folder-id", folder_id, *args]
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def upload(
    path: Path,
    *,
    folder_id: str = DEFAULT_FOLDER_ID,
    snapshot: str | None = None,
    keep_history: bool = True,
) -> None:
    """xlsx を最新版として置き、履歴も残す。"""
    _rclone(["copyto", str(path), f"{REMOTE}{path.name}"], folder_id)
    log.info("アップロード完了: %s", path.name)

    if keep_history and snapshot:
        historical = f"{path.stem}_{snapshot}{path.suffix}"
        _rclone(["copyto", str(path), f"{REMOTE}history/{historical}"], folder_id)
        log.info("履歴を保存: history/%s", historical)


def listing(folder_id: str = DEFAULT_FOLDER_ID) -> str:
    return _rclone(["lsl", REMOTE], folder_id).stdout
