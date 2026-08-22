"""クロール用の共通 HTTP クライアント。

やっていること:

* ホストごとにレートリミット（既定 2 秒、`HOST_DELAY` で個別上書き）
* 素性のわかる User-Agent
* 429 / 5xx の指数バックオフ・リトライ
* スナップショット日付をキーにしたディスクキャッシュ
  （同じ週にパイプラインを何度回しても相手サイトを叩き直さない）
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from pathlib import Path
from typing import Any

import requests
from urllib.parse import urlparse

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/NyxFoundation/kakaku-ai"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/126.0.0.0 Safari/537.36 (+{REPO_URL}; research crawler)"
)

DEFAULT_DELAY = 2.0
HOST_DELAY: dict[str, float] = {
    "auctions.yahoo.co.jp": 2.5,
    "www.carsensor.net": 2.0,
    "kakaku.com": 2.5,
    "minkara.carview.co.jp": 2.5,
    "renrakuda.mlit.go.jp": 1.5,
    "www.goo-net.com": 2.5,
    "jmty.jp": 2.0,
}

MAX_RETRIES = 3
RETRYABLE = {429, 500, 502, 503, 504}


class Fetcher:
    """レート制限とキャッシュ付きの GET。

    `cache_dir` を渡すと `<cache_dir>/<snapshot>/<host>/<hash>.bin` に生レスポンスを
    保存し、次回以降はそこから返す。生バイトを持っておくとパーサだけ直して
    再集計する、というのがネットワークなしでできる。
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        snapshot: str | None = None,
        *,
        use_cache: bool = True,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
            }
        )
        self.cache_dir = cache_dir
        self.snapshot = snapshot
        self.use_cache = use_cache and cache_dir is not None and snapshot is not None
        self._last_hit: dict[str, float] = {}

    # ------------------------------------------------------------------ cache

    def _cache_path(self, url: str, params: Any) -> Path | None:
        if not self.use_cache:
            return None
        assert self.cache_dir is not None and self.snapshot is not None
        host = urlparse(url).netloc
        key = hashlib.sha256(f"{url}|{params!r}".encode()).hexdigest()[:32]
        return self.cache_dir / self.snapshot / host / f"{key}.bin"

    # ----------------------------------------------------------------- limits

    def _wait(self, host: str) -> None:
        delay = HOST_DELAY.get(host, DEFAULT_DELAY)
        last = self._last_hit.get(host)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        # 一定間隔ちょうどで叩き続けないよう軽くばらす
        time.sleep(random.uniform(0, 0.4))
        self._last_hit[host] = time.monotonic()

    # -------------------------------------------------------------------- get

    def get_bytes(self, url: str, params: dict[str, Any] | None = None) -> bytes:
        cache_path = self._cache_path(url, params)
        if cache_path is not None and cache_path.exists():
            log.debug("cache hit %s", url)
            return cache_path.read_bytes()

        host = urlparse(url).netloc
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            self._wait(host)
            try:
                resp = self.session.get(url, params=params, timeout=45)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("GET %s failed (%s/%s): %s", url, attempt, MAX_RETRIES, exc)
                time.sleep(2**attempt)
                continue

            if resp.status_code in RETRYABLE:
                last_error = requests.HTTPError(f"HTTP {resp.status_code}")
                log.warning(
                    "GET %s -> %s (%s/%s), backing off",
                    url,
                    resp.status_code,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(2**attempt * 2)
                continue

            resp.raise_for_status()
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(resp.content)
            return resp.content

        raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} attempts") from last_error

    def get_text(self, url: str, params: dict[str, Any] | None = None, encoding: str = "utf-8") -> str:
        return self.get_bytes(url, params).decode(encoding, "replace")
