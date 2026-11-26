from collections import defaultdict
from collections.abc import KeysView
from typing import Any
from urllib.parse import urljoin

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "SPRTSBITE"

CACHE_FILE = Cache(TAG, exp=14_400)

BASE_URL = "https://sportsbite.org"


def process_event(url: str, url_num: int) -> str:
    s = url.replace("/embed/", "/cdn/")

    log.info(f"URL {url_num}) Captured M3U8")

    return f"{s}.m3u8?cdn=1"


async def get_events(cached_keys: KeysView[str]) -> dict[str, dict[str, str | float]]:
    now = Time.rn()

    events = {}

    if not (
        api_req := await network.request(
            urljoin(BASE_URL, "api/forestgump/matches"),
            log=log,
        )
    ):
        return events

    api_data: list[dict[str, Any]] = api_req.json()

    start_ts = now.delta(minutes=-30).timestamp()
    end_ts = now.delta(minutes=30).timestamp()

    counter: dict[str, int] = defaultdict(int)

    i = 1

    for game_event in api_data.get("days", [{}]):
        for game_info in game_event.get("events", [{}]):
            if not all(
                values := [
                    game_info.get(x)
                    for x in (
                        "title",
                        "category",
                        "kickoffMs",
                        "streams",
                    )
                ]
            ):
                continue

            name, sport, event_ts, event_streams = values

            event_ts = int(f"{event_ts}"[:-3])

            if not start_ts <= event_ts <= end_ts:
                continue

            tvg_id, logo = leagues.get_tvg_info(sport, name)

            for stream_info in event_streams:
                counter[name] += 1

                if stream_info.get("source") != "selfhost":
                    continue

                elif not (url := stream_info.get("manifest_url")):
                    continue

                elif (
                    key := f"[{sport}] {name} {counter[name]} ({TAG})"
                ) in cached_keys:
                    continue

                events[key] = {
                    "source": process_event(url, i),
                    "logo": logo,
                    "refer": BASE_URL,
                    "timestamp": event_ts,
                    "tvg-id": tvg_id or "Live.Event.us",
                }

                i += 1

    return events


async def scrape() -> None:
    cached_sources = CACHE_FILE.load()

    valid_count = len(
        valid_sources := {k: v for k, v in cached_sources.items() if v["source"]}
    )

    urls.update(valid_sources)

    log.info(f"Loaded {valid_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    urls.update(await get_events(cached_sources.keys()))

    (
        log.info(f"Collected and cached {new_count} new event(s)")
        if (new_count := len(urls) - valid_count)
        else log.info("No new events found")
    )

    CACHE_FILE.write(urls)
