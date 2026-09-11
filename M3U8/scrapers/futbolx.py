import asyncio
import re
from collections.abc import KeysView
from itertools import chain
from urllib.parse import urljoin

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "FUTBOLX"

CACHE_FILE = Cache(TAG, exp=19_800)

BASE_URL = "https://futbol-x.xyz"

SPORT_URLS = [
    urljoin(BASE_URL, f"api/{sport}.json")
    for sport in [
        "americanfootball",
        "baseball",
        "basketball",
        "darts",
        "fights",
        "football",
        # "golf",
        "motorsports",
        # "nhl",
        "others",
        # "rugby",
        "tennis",
        "wrestling",
    ]
]


async def get_events(cached_keys: KeysView[str]) -> dict[str, dict[str, str | float]]:
    events = {}

    tasks = [network.request(url, log=log) for url in SPORT_URLS]

    results = await asyncio.gather(*tasks)

    if not (
        api_data := [
            *chain.from_iterable(r.json().get("streams", {}) for r in results if r)
        ]
    ):
        return events

    now = Time.rn()

    ptrn = re.compile(r"^(https?:\/\/)?(www\.)?.*\.m3u8$", re.I)

    for event in api_data:
        if not (streams := event.get("streams")):
            continue

        for event_info in streams:
            if not all(
                values := [
                    event_info.get(k)
                    for k in (
                        "name",
                        "tag",
                        "starts_at",
                        "streams",
                    )
                ]
            ):
                continue

            name, sport, event_time, event_streams = values

            event_dt = Time.fromisoformat(event_time).to_tz("EST")

            if event_dt.date() != now.date():
                continue

            for stream_info in event_streams:
                if not (source := stream_info.get("url")):
                    continue

                elif not ptrn.search(source):
                    continue

                url_title = stream_info["title"]

                if (key := f"[{sport}] {name} | {url_title} ({TAG})") in cached_keys:
                    continue

                tvg_id, logo = leagues.get_tvg_info(sport, name)

                events[key] = {
                    "source": source,
                    "logo": logo,
                    "refer": BASE_URL,
                    "timestamp": now.timestamp(),
                    "tvg-id": tvg_id or "Live.Event.us",
                }

    return events


async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_count = len(
        valid_urls := {k: v for k, v in cached_urls.items() if v["source"]}
    )

    urls.update(valid_urls)

    log.info(f"Loaded {valid_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    urls.update(await get_events(cached_urls.keys()))

    (
        log.info(f"Collected and cached {new_count} new event(s)")
        if (new_count := len(urls) - valid_count)
        else log.info("No new events found")
    )

    CACHE_FILE.write(urls)
