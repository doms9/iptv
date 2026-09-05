from collections.abc import KeysView
from functools import partial
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import Browser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "PLYFST"

CACHE_FILE = Cache(TAG, exp=5_400)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL = "https://playfa.st"


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    now = Time.rn()

    if not (api_data := API_FILE.load(per_entry=False)):
        log.info("Refreshing API cache")

        api_data = {"timestamp": now.timestamp()}

        if r := await network.request(
            urljoin(BASE_URL, "ch.php"),
            params={"id": 1, "schedule": 1},
            log=log,
        ):
            api_data: dict[str, list[dict[str, Any]]] = r.json()

            api_data["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    events: list[Event] = []

    lang_map = {
        "GB": "EN",
        "US": "EN",
    }

    start_dt = now.delta(hours=-3)
    end_dt = now.delta(minutes=30)

    for info in api_data.get("matches", []):
        event_name, sport = info["matchstr"], info["league"]

        event_dt = Time.from_ts(int(f'{info["startTimestamp"]}'[:-3]))

        if not start_dt <= event_dt <= end_dt:
            continue

        if not (event_channels := info.get("channels")):
            continue

        event_urls: dict[int, str] = {
            channel["number"]: lang_map.get(channel["language"], channel["language"])
            for channel in event_channels
        }

        events.extend(
            Event(
                sport=sport,
                name=f"{event_name} | {lang}",
                link=f"https://s1.kora.st/ch.php?id={event_num}",
                timestamp=now.timestamp(),
            )
            for event_num, lang in event_urls.items()
            if f"[{sport}] {event_name} | {lang} ({TAG})" not in cached_keys
        )

    return events


async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["source"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                async with network.event_page(context) as page:
                    handler = partial(
                        network.process_event,
                        url=ev.link,
                        url_num=i,
                        page=page,
                        log=log,
                    )

                    source = await network.safe_process(
                        handler,
                        url_num=i,
                        semaphore=network.PW_S,
                        log=log,
                    )

                    tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

                    key = f"[{ev.sport}] {ev.name} ({TAG})"

                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": "https://forgemindly.com/",
                        "timestamp": ev.timestamp,
                        "tvg-id": tvg_id or "Live.Event.us",
                        "link": ev.link,
                    }

                    cached_urls[key] = entry

                    if source:
                        valid_count += 1

                        urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
