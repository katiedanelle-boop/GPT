"""Utilities to export a user's Steam library alongside ProtonDB rankings.

The script expects a Steam Web API key and a 64-bit SteamID belonging to the
account whose library should be exported.  It queries the Steam Web API for the
list of owned games and then enriches that information with ProtonDB summary
metrics before writing a CSV file sorted by the ProtonDB tier ranking.

Example usage::

    python steam_protondb_export.py --steamid 76561197960435530 --api-key $STEAM_KEY \
        --output my_library.csv

The resulting CSV is ordered from best ProtonDB tier to worst, with secondary
sorting by ProtonDB score and finally the game name.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import requests
from requests import Response, Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ProtonDB uses a tier system that mirrors the metals used on the site.
# The lower the number, the better the ranking when sorting.
PROTONDB_TIER_ORDER: Dict[str, int] = {
    "native": 0,
    "platinum": 1,
    "gold": 2,
    "silver": 3,
    "bronze": 4,
    "borked": 5,
    "pending": 6,
}

# Some titles do not yet have ProtonDB coverage, in which case we sort them to
# the bottom of the output.
DEFAULT_TIER_ORDER = max(PROTONDB_TIER_ORDER.values()) + 1

STEAM_GET_OWNED_GAMES = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
PROTONDB_SUMMARY_URL = "https://www.protondb.com/api/v1/reports/summaries/{appid}.json"


@dataclass
class ProtonDBRecord:
    """Container for the ProtonDB information we care about."""

    appid: int
    name: str
    playtime_forever: int
    tier: Optional[str]
    score: Optional[float]
    confidence: Optional[str]
    trending_tier: Optional[str]
    best_reported_tier: Optional[str]
    reports: Optional[int]
    last_reported_time: Optional[int]

    @property
    def hours_played(self) -> float:
        """Return the amount of playtime in hours (Steam reports minutes)."""

        return round(self.playtime_forever / 60.0, 2)

    @property
    def tier_order(self) -> int:
        """Return the numeric ordering for the ProtonDB tier."""

        if not self.tier:
            return DEFAULT_TIER_ORDER

        return PROTONDB_TIER_ORDER.get(self.tier.lower(), DEFAULT_TIER_ORDER)


class SteamAPIError(RuntimeError):
    """Raised when the Steam Web API responds with an error payload."""


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def build_retrying_session(total_retries: int = 3, backoff_factor: float = 0.3) -> Session:
    """Return a :class:`requests.Session` with retry/backoff configured."""

    retry = Retry(
        total=total_retries,
        read=total_retries,
        connect=total_retries,
        status=total_retries,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        backoff_factor=backoff_factor,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "steam-protondb-export/1.0 (+https://github.com/)\n",
            "Accept": "application/json",
        }
    )
    return session


def fetch_owned_games(session: Session, api_key: str, steamid: str, include_free: bool) -> List[Dict[str, object]]:
    """Query the Steam Web API for the list of games owned by ``steamid``."""

    params = {
        "key": api_key,
        "steamid": steamid,
        "include_appinfo": True,
        "include_played_free_games": include_free,
    }

    logging.debug("Requesting owned games from Steam API")
    response = session.get(STEAM_GET_OWNED_GAMES, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()

    if "response" not in payload:
        raise SteamAPIError("Unexpected response payload from Steam API")

    return payload["response"].get("games", [])


def fetch_protondb_summary(session: Session, appid: int) -> Optional[Dict[str, object]]:
    """Retrieve the ProtonDB summary information for ``appid``."""

    url = PROTONDB_SUMMARY_URL.format(appid=appid)
    logging.debug("Requesting ProtonDB summary for app %s", appid)
    try:
        response: Response = session.get(url, timeout=30)
        if response.status_code == 404:
            logging.debug("No ProtonDB summary found for app %s", appid)
            return None
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logging.warning("Failed to fetch ProtonDB summary for %s: %s", appid, exc)
        return None


def iter_protondb_records(
    session: Session,
    games: Iterable[Dict[str, object]],
    rate_limit: float,
) -> Iterable[ProtonDBRecord]:
    """Yield :class:`ProtonDBRecord` instances for the provided ``games``."""

    for game in games:
        appid = int(game["appid"])
        name = str(game.get("name", "<unknown>"))
        playtime = int(game.get("playtime_forever", 0))

        summary = fetch_protondb_summary(session, appid) or {}

        record = ProtonDBRecord(
            appid=appid,
            name=name,
            playtime_forever=playtime,
            tier=summary.get("tier"),
            score=summary.get("score"),
            confidence=summary.get("confidence"),
            trending_tier=summary.get("trendingTier"),
            best_reported_tier=summary.get("bestReportedTier"),
            reports=summary.get("reports"),
            last_reported_time=summary.get("lastReportedTime"),
        )
        yield record

        if rate_limit:
            logging.debug("Sleeping for %ss between ProtonDB requests", rate_limit)
            time.sleep(rate_limit)


def write_csv(records: Iterable[ProtonDBRecord], output_path: str) -> None:
    """Write the supplied ProtonDB records to ``output_path`` in CSV format."""

    fieldnames = [
        "appid",
        "name",
        "hours_played",
        "protondb_tier",
        "protondb_score",
        "protondb_confidence",
        "protondb_trending_tier",
        "protondb_best_reported_tier",
        "protondb_reports",
        "protondb_last_reported_time",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for record in records:
            writer.writerow(
                {
                    "appid": record.appid,
                    "name": record.name,
                    "hours_played": record.hours_played,
                    "protondb_tier": record.tier or "unknown",
                    "protondb_score": record.score if record.score is not None else "",
                    "protondb_confidence": record.confidence or "",
                    "protondb_trending_tier": record.trending_tier or "",
                    "protondb_best_reported_tier": record.best_reported_tier or "",
                    "protondb_reports": record.reports if record.reports is not None else "",
                    "protondb_last_reported_time": record.last_reported_time if record.last_reported_time is not None else "",
                }
            )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key", default=os.getenv("STEAM_API_KEY"), help="Steam Web API key")
    parser.add_argument("--steamid", default=os.getenv("STEAM_ID"), help="64-bit SteamID to inspect")
    parser.add_argument(
        "--output",
        default="steam_protondb_export.csv",
        help="Destination CSV path (default: %(default)s)",
    )
    parser.add_argument(
        "--include-free",
        action="store_true",
        help="Include free-to-play titles in the export",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=0.3,
        help="Seconds to sleep between ProtonDB requests (default: %(default)s)",
    )
    parser.add_argument(
        "--no-rate-limit",
        action="store_true",
        help="Disable sleeping between ProtonDB requests",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if not args.api_key:
        raise SystemExit("A Steam Web API key must be provided via --api-key or STEAM_API_KEY")
    if not args.steamid:
        raise SystemExit("A SteamID must be provided via --steamid or STEAM_ID")


def sort_records(records: Iterable[ProtonDBRecord]) -> List[ProtonDBRecord]:
    return sorted(
        records,
        key=lambda record: (
            record.tier_order,
            -record.score if record.score is not None else float("inf"),
            record.name.lower(),
        ),
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    validate_args(args)
    configure_logging(args.verbose)

    if args.no_rate_limit:
        rate_limit = 0.0
    else:
        rate_limit = max(0.0, args.rate_limit)

    session = build_retrying_session()

    logging.info("Fetching owned games for SteamID %s", args.steamid)
    try:
        games = fetch_owned_games(session, args.api_key, args.steamid, args.include_free)
    except requests.HTTPError as exc:
        logging.error("Steam API request failed: %s", exc)
        return 1
    except SteamAPIError as exc:
        logging.error("Steam API returned an unexpected payload: %s", exc)
        return 1

    if not games:
        logging.warning("No games found for the supplied SteamID")
        return 0

    logging.info("Retrieved %d games; fetching ProtonDB data", len(games))

    records = list(iter_protondb_records(session, games, rate_limit))
    sorted_records = sort_records(records)

    logging.info("Writing %d rows to %s", len(sorted_records), args.output)
    write_csv(sorted_records, args.output)

    logging.info("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
