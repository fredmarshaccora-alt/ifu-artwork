"""Vendored copy of the Onshape REST client.

The dev machine loads this from a sibling project
(C:\\Users\\...\\Projects\\onshape-analytics); that path doesn't exist on
a server, so this self-contained copy ships in the repo and is used as
the fallback (see onshape_client.py).  Credentials come from
ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY (Render secrets in prod).

Keep in sync with onshape_analytics/client.py if that upstream changes.
Only stdlib + requests + python-dotenv (both already in requirements).
"""

import os
import time
import logging
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


class OnshapeClient:
    """Thin wrapper around the Onshape REST API using Basic auth (API keys)."""

    def __init__(
        self,
        access_key: str | None = None,
        secret_key: str | None = None,
        base_url: str | None = None,
        api_version: str | None = None,
    ):
        self.access_key = access_key or os.getenv("ONSHAPE_ACCESS_KEY", "")
        self.secret_key = secret_key or os.getenv("ONSHAPE_SECRET_KEY", "")
        self.base_url = (base_url or os.getenv(
            "ONSHAPE_BASE_URL", "https://cad.onshape.com")).rstrip("/")
        self.api_version = api_version or os.getenv("ONSHAPE_API_VERSION", "v10")

        if not self.access_key or not self.secret_key:
            raise ValueError(
                "Onshape API keys not configured. "
                "Set ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY in the "
                "environment (or .env) or pass them directly."
            )

        self._session = requests.Session()
        self._session.auth = (self.access_key, self.secret_key)
        self._session.headers.update({
            "Accept": "application/json;charset=UTF-8;qs=0.09",
            "Content-Type": "application/json;charset=UTF-8;qs=0.09",
        })

        ssl_verify = os.getenv("ONSHAPE_SSL_VERIFY", "true").lower()
        if ssl_verify in ("false", "0", "no"):
            self._session.verify = False
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @property
    def api_base(self) -> str:
        return f"{self.base_url}/api/{self.api_version}"

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.api_base}{path}"

    def get(self, path: str, params: dict | None = None, **kwargs) -> Any:
        return self._request("GET", path, params=params, **kwargs)

    def post(self, path: str, json: dict | None = None, **kwargs) -> Any:
        return self._request("POST", path, json=json, **kwargs)

    def _request(self, method: str, path: str, retries: int = 3, **kwargs) -> Any:
        url = self._url(path)
        for attempt in range(retries):
            resp = self._session.request(method, url, **kwargs)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 5))
                log.warning("Rate limited. Waiting %ds (attempt %d/%d)",
                            retry_after, attempt + 1, retries)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            if resp.content:
                return resp.json()
            return None
        raise RuntimeError(f"Request failed after {retries} retries: {method} {url}")

    def get_paginated(self, path: str, params: dict | None = None,
                      limit: int = 50, max_items: int = 500) -> list:
        params = dict(params or {})
        params.setdefault("offset", 0)
        params.setdefault("limit", limit)
        all_items: list = []
        while True:
            data = self.get(path, params=params)
            if isinstance(data, list):
                all_items.extend(data)
                if len(data) < limit:
                    break
            elif isinstance(data, dict):
                items = data.get("items", data.get("elements", []))
                all_items.extend(items)
                if data.get("next"):
                    params["offset"] = params["offset"] + limit
                elif len(items) < limit:
                    break
                else:
                    params["offset"] = params["offset"] + limit
            else:
                break
            if len(all_items) >= max_items:
                break
        return all_items[:max_items]

    def test_connection(self) -> dict:
        return self.get("/users/sessioninfo")
