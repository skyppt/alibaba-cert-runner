"""Read-only client for Alibaba International Station IOP product APIs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent / ".env")


class AlibabaApiError(RuntimeError):
    """Raised when Alibaba rejects an API call or returns an error payload."""


class AlibabaIopClient:
    def __init__(self) -> None:
        self.app_key = os.environ.get("ALIBABA_APP_KEY", "").strip()
        self.app_secret = os.environ.get("ALIBABA_APP_SECRET", "").strip()
        self.access_token = os.environ.get("ALIBABA_ACCESS_TOKEN", "").strip()
        self.base_url = os.environ.get("ALIBABA_API_BASE_URL", "https://open-api.alibaba.com").rstrip("/")
        self.timeout = int(os.environ.get("ALIBABA_API_TIMEOUT", "60"))
        self.retries = int(os.environ.get("ALIBABA_API_RETRIES", "5"))
        if not all((self.app_key, self.app_secret, self.access_token)):
            raise AlibabaApiError("Alibaba App Key, App Secret, and access token must be configured before syncing.")

    def _sign(self, api_name: str, parameters: dict[str, str]) -> str:
        # Alibaba's TOP SDK signs the sorted request parameters; the TOP signing prefix is empty.
        signing_text = "".join(f"{key}{parameters[key]}" for key in sorted(parameters))
        return hmac.new(self.app_secret.encode("utf-8"), signing_text.encode("utf-8"), hashlib.sha256).hexdigest().upper()

    def call(self, api_name: str, parameters: dict[str, str]) -> dict:
        request_parameters = {
            **{key: str(value) for key, value in parameters.items() if value not in (None, "")},
            "app_key": self.app_key,
            "v": "2.0",
            "timestamp": str(int(time.time() * 1000)),
            "method": api_name,
            "format": "json",
            "session": self.access_token,
            "partner_id": "iop-sdk-java-20181207",
            "sign_method": "sha256",
        }
        request_parameters["sign"] = self._sign(api_name, request_parameters)
        request = Request(
            f"{self.base_url}/sync?method={api_name}",
            data=json.dumps(request_parameters).encode("utf-8"),
            headers={"Content-Type": "application/json;charset=utf-8", "Accept": "application/json"},
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                raise AlibabaApiError(f"Alibaba API returned HTTP {error.code}: {detail[:500]}") from error
            except (TimeoutError, socket.timeout, URLError) as error:
                last_error = error
                if attempt == self.retries:
                    reason = getattr(error, "reason", error)
                    raise AlibabaApiError(f"Alibaba API connection failed after {self.retries} attempts: {reason}") from error
                time.sleep(attempt * 2)
        else:
            raise AlibabaApiError(f"Alibaba API connection failed: {last_error}")

        if str(payload.get("code", "0")) != "0":
            raise AlibabaApiError(payload.get("message") or payload.get("msg") or json.dumps(payload, ensure_ascii=False))
        return payload

    def list_products(self, page: int, page_size: int = 30, modified_from: str | None = None, modified_to: str | None = None) -> dict:
        return self.call(
            "alibaba.icbu.product.list",
            {
                "current_page": page,
                "page_size": page_size,
                "language": "ENGLISH",
                "status": "approved",
                "display": "Y",
                "gmt_modified_from": modified_from,
                "gmt_modified_to": modified_to,
            },
        )

def response_body(payload: dict, api_name: str) -> dict:
    """Alibaba returns an API-specific response envelope; support both documented shapes."""
    return payload.get(f"{api_name.replace('.', '_')}_response") or payload.get("result") or payload
