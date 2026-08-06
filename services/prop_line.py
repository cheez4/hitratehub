from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import requests
from requests import Response
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_BASE_URL = "https://api.prop-line.com/v1"
DEFAULT_TIMEOUT = 30


class PropLineError(RuntimeError):
    pass


class PropLineAuthError(PropLineError):
    pass


class PropLineRequestError(PropLineError):
    def __init__(self, message, status_code=None, response_text=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(frozen=True)
class PropLineConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    timeout: int = DEFAULT_TIMEOUT


class PropLineClient:
    def __init__(self, api_key=None, *, base_url=DEFAULT_BASE_URL, timeout=DEFAULT_TIMEOUT, session=None):
        resolved_key = (api_key or os.getenv("PROP_LINE_API_KEY") or "").strip()
        if not resolved_key:
            raise PropLineAuthError("PROP_LINE_API_KEY is missing.")

        self.config = PropLineConfig(
            api_key=resolved_key,
            base_url=base_url.rstrip("/"),
            timeout=timeout,
        )
        self.session = session or self._build_session()
        self.session.headers.update({
            "X-API-Key": self.config.api_key,
            "Accept": "application/json",
            "User-Agent": "HitRateHub/1.0",
        })

    @staticmethod
    def _build_session():
        session = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    @staticmethod
    def _clean_params(params):
        if not params:
            return {}
        cleaned = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                cleaned[key] = str(value).lower()
            elif isinstance(value, (list, tuple, set)):
                cleaned[key] = ",".join(str(item) for item in value)
            else:
                cleaned[key] = value
        return cleaned

    @staticmethod
    def _raise_for_response(response: Response):
        if response.ok:
            return
        preview = response.text[:1000]
        if response.status_code in {401, 403}:
            raise PropLineAuthError(
                f"Prop-Line rejected the request with HTTP {response.status_code}."
            )
        raise PropLineRequestError(
            f"Prop-Line returned HTTP {response.status_code}.",
            status_code=response.status_code,
            response_text=preview,
        )

    def _request(self, method, path, *, params=None):
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        try:
            response = self.session.request(
                method=method,
                url=url,
                params=self._clean_params(params),
                timeout=self.config.timeout,
            )
        except requests.Timeout as exc:
            raise PropLineRequestError("Prop-Line request timed out.") from exc
        except requests.RequestException as exc:
            raise PropLineRequestError(f"Prop-Line request failed: {exc}") from exc

        self._raise_for_response(response)

        try:
            return response.json()
        except ValueError as exc:
            raise PropLineRequestError(
                "Prop-Line returned a non-JSON response.",
                status_code=response.status_code,
                response_text=response.text[:500],
            ) from exc

    def healthcheck(self):
        events = self.get_events("baseball_mlb")
        return {"connected": True, "event_count": len(events)}

    def get_events(self, sport, *, live=None):
        data = self._request(
            "GET",
            f"sports/{sport}/events",
            params={"live": live},
        )
        if not isinstance(data, list):
            raise PropLineRequestError("Expected events to return a list.")
        return data

    def get_event_odds(self, sport, event_id, *, markets=None, bookmakers=None):
        data = self._request(
            "GET",
            f"sports/{sport}/events/{event_id}/odds",
            params={
                "markets": list(markets) if markets else None,
                "bookmakers": list(bookmakers) if bookmakers else None,
            },
        )
        if not isinstance(data, dict):
            raise PropLineRequestError("Expected odds to return an object.")
        return data

    def get_scores(self, sport, *, days_from=None):
        data = self._request(
            "GET",
            f"sports/{sport}/scores",
            params={"daysFrom": days_from},
        )
        if not isinstance(data, list):
            raise PropLineRequestError("Expected scores to return a list.")
        return data

    def get_results(self, sport, event_id, *, markets=None, bookmakers=None):
        data = self._request(
            "GET",
            f"sports/{sport}/events/{event_id}/results",
            params={
                "markets": list(markets) if markets else None,
                "bookmakers": list(bookmakers) if bookmakers else None,
            },
        )
        if not isinstance(data, dict):
            raise PropLineRequestError("Expected results to return an object.")
        return data

    def get_stats(self, sport, event_id):
        data = self._request(
            "GET",
            f"sports/{sport}/events/{event_id}/stats",
        )
        if not isinstance(data, dict):
            raise PropLineRequestError("Expected stats to return an object.")
        return data


def get_default_client():
    return PropLineClient()
