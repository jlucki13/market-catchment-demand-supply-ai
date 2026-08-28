"""One HTTP helper for every external call.

Centralised so retry policy, timeouts, and error reporting are decided once.
The failure mode that matters here is a partial run: a 429 halfway through
enrichment leaves a catchment with some competitors priced and some not, and the
Supply Index silently understates. So transient failures retry with backoff, and
permanent ones raise with the response body attached rather than a bare status.
"""

from __future__ import annotations

import random
import time
from typing import Any

import requests

DEFAULT_TIMEOUT = 30

#: Statuses worth retrying. 429 is rate limiting, 5xx is Google or Census having
#: a moment. A 400 or 403 will fail identically forever, so it raises at once.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 1.0


class ApiError(RuntimeError):
    """An external API call that will not succeed on retry.

    Carries the response body, because every API in this pipeline puts the
    actionable detail there rather than in the status code -- Google's
    "Type is not supported, type: dry_cleaner" is a 400 like any other.
    """

    def __init__(self, service: str, status: int, body: str, url: str = ""):
        self.service, self.status, self.body, self.url = service, status, body, url
        super().__init__(f"{service} returned HTTP {status}: {body[:400]}")


def request(
    method: str,
    url: str,
    *,
    service: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_attempts: int = MAX_ATTEMPTS,
    **kwargs: Any,
) -> requests.Response:
    """Issue a request, retrying transient failures with jittered backoff.

    Jitter matters when enriching a catchment: a synchronous retry loop over 30
    competitors that all hit the same rate limit will otherwise retry in
    lockstep and hit it again together.
    """
    last: Exception | None = None

    for attempt in range(max_attempts):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last = exc
            if attempt == max_attempts - 1:
                raise ApiError(service, 0, f"connection failed: {exc}", url) from exc
        else:
            if response.status_code == 200:
                return response
            if response.status_code not in RETRYABLE_STATUS:
                raise ApiError(service, response.status_code, response.text, url)
            last = ApiError(service, response.status_code, response.text, url)
            if attempt == max_attempts - 1:
                raise last

        delay = BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 0.4)
        time.sleep(delay)

    raise last if last else ApiError(service, 0, "exhausted retries", url)


def get_json(url: str, *, service: str, **kwargs: Any) -> dict:
    return request("GET", url, service=service, **kwargs).json()


def post_json(url: str, *, service: str, **kwargs: Any) -> dict:
    return request("POST", url, service=service, **kwargs).json()
