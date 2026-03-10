"""
CHIMERA FSU Client
==================
Drop-in replacement for BetfairClient that fetches data from the FSU
(Fractional Services Unit) instead of the live Betfair Exchange API.

Used by FSU3 (standalone backtest service) to replay historic market data.

  client = FSUClient(base_url=FSU_URL, date="2025-07-13")
  client.set_virtual_time("2025-07-13T12:00:00Z")

  markets = client.get_todays_win_markets(countries=["GB", "IE"])
  runners, valid = client.get_market_prices(market_id)
  book = client.get_market_book_full(market_id)

The virtual_time property controls which point in the historic
timeline the FSU reconstructs state for.  Advance it to replay
market evolution through a race day.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from rules import Runner

logger = logging.getLogger("fsu_client")

FSU_BASE_URL = os.getenv("FSU_URL", "https://fsu.thync.online")


class FSUClient:
    """
    Fetches historic Betfair market data from the FSU service.
    """

    def __init__(
        self,
        base_url: str = FSU_BASE_URL,
        date: Optional[str] = None,
        virtual_time: Optional[str] = None,
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._virtual_time: Optional[str] = virtual_time
        self.timeout = timeout
        self._session = requests.Session()

    # ──────────────────────────────────────────────
    #  VIRTUAL CLOCK
    # ──────────────────────────────────────────────

    @property
    def virtual_time(self) -> str:
        if self._virtual_time:
            return self._virtual_time
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def set_virtual_time(self, iso_timestamp: str) -> None:
        self._virtual_time = iso_timestamp
        logger.info(f"FSU virtual time → {iso_timestamp}")

    def advance_virtual_time(self, seconds: int) -> None:
        ts = self._parse_ts(self.virtual_time)
        new_ts = ts + seconds * 1000
        iso = datetime.fromtimestamp(new_ts / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.set_virtual_time(iso)

    # ──────────────────────────────────────────────
    #  GCP IDENTITY TOKEN  (Cloud Run → Cloud Run)
    # ──────────────────────────────────────────────

    def _fetch_identity_token(self) -> Optional[str]:
        meta_url = (
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            f"service-accounts/default/identity?audience={self.base_url}"
        )
        try:
            resp = requests.get(
                meta_url,
                headers={"Metadata-Flavor": "Google"},
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.text.strip()
        except Exception:
            pass
        return None

    def _refresh_auth_header(self) -> None:
        token = self._fetch_identity_token()
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})
            logger.info("FSUClient: GCP identity token set on session")
        else:
            logger.info("FSUClient: no metadata server — running unauthenticated (local dev)")

    # ──────────────────────────────────────────────
    #  AUTH
    # ──────────────────────────────────────────────

    def login(self) -> bool:
        self._refresh_auth_header()
        logger.info(f"FSUClient: backtest mode for date={self.date}")
        return True

    # ──────────────────────────────────────────────
    #  MARKET DISCOVERY
    # ──────────────────────────────────────────────

    def get_todays_win_markets(
        self, countries: Optional[list[str]] = None
    ) -> list[dict]:
        countries = countries or ["GB", "IE"]
        params = {
            "date": self.date,
            "market_type": "WIN",
            "countries": ",".join(countries),
        }
        resp = self._get("/api/markets", params=params)
        if resp is None:
            return []

        markets = []
        for m in resp.get("markets", []):
            markets.append({
                "market_id": m["market_id"],
                "market_name": m["market_name"],
                "venue": m["venue"],
                "country": m["country"],
                "race_time": m["race_time"],
                "runners": [
                    {
                        "selection_id": r["selection_id"],
                        "runner_name": r["runner_name"],
                        "handicap": r.get("handicap", 0.0),
                        "sort_priority": r["sort_priority"],
                    }
                    for r in m.get("runners", [])
                ],
            })

        logger.info(
            f"FSU: {len(markets)} WIN markets for {self.date} ({'/'.join(countries)})"
        )
        return markets

    # ──────────────────────────────────────────────
    #  PRICE RETRIEVAL
    # ──────────────────────────────────────────────

    def get_market_prices(self, market_id: str) -> tuple[list[Runner], bool]:
        params = {
            "timestamp": self.virtual_time,
            "date": self.date,
        }
        resp = self._get(f"/api/markets/{market_id}/prices", params=params)
        if resp is None:
            return [], False

        status = resp.get("status", "OPEN")
        in_play = resp.get("in_play", False)

        if status != "OPEN":
            logger.warning(f"FSU: market {market_id} status={status} — skipping")
            return [], False

        if in_play:
            logger.warning(f"FSU: market {market_id} is IN-PLAY — skipping (pre-off only)")
            return [], False

        runners = []
        for r in resp.get("runners", []):
            runner = Runner(
                selection_id=r["selection_id"],
                runner_name=r.get("runner_name", f"Runner {r['selection_id']}"),
                handicap=r.get("handicap", 0.0),
                status=r.get("status", "ACTIVE"),
            )
            runner.best_available_to_lay = r.get("best_available_to_lay")
            runner.best_available_to_back = r.get("best_available_to_back")
            runners.append(runner)

        return runners, True

    # ──────────────────────────────────────────────
    #  RACE RESULT
    # ──────────────────────────────────────────────

    def get_race_result(self, market_id: str) -> Optional[dict]:
        params = {
            "timestamp": "2099-01-01T00:00:00Z",
            "date": self.date,
        }
        resp = self._get(f"/api/markets/{market_id}/prices", params=params)
        if resp is None:
            return None

        md_status = resp.get("status", "OPEN")
        settled = md_status == "CLOSED"

        winner_id = None
        for r in resp.get("runners", []):
            if r.get("status") == "WINNER":
                winner_id = r["selection_id"]
                break

        return {"settled": settled, "winner_selection_id": winner_id}

    # ──────────────────────────────────────────────
    #  TIMELINE
    # ──────────────────────────────────────────────

    def get_market_timeline(self, market_id: str) -> Optional[dict]:
        resp = self._get(f"/api/markets/{market_id}/timeline", params={"date": self.date})
        return resp

    # ──────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"FSU HTTP error {e.response.status_code} for {url}: {e.response.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"FSU request failed for {url}: {e}")
            return None

    @staticmethod
    def _parse_ts(iso: str) -> int:
        ts = iso.strip().replace("Z", "+00:00")
        return int(datetime.fromisoformat(ts).timestamp() * 1000)
