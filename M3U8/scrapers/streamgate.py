import asyncio
import json
import re
from collections.abc import KeysView
from functools import partial
from itertools import chain
from typing import Any
from urllib.parse import urljoin

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "STRMGATE"

CACHE_FILE = Cache(TAG, exp=10_800)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL = "https://embedme.st"

SPORT_ENDPOINTS = {
    # "cfb",
    # "mlb",
    "nba",
    "nfl",
    "nhl",
    "soccer",
    "ufc",
}


def clean_m3u(s: str) -> str:
    return re.sub(r"\.live\n", ".pro", s)


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (
        event_data := await network.request(
            url,
            url_num,
            headers={"Referer": BASE_URL},
            timeout=httpx.Timeout(25.0),
            log=log,
        )
    ):
        return nones

    soup = HTMLParser(event_data.content)

    ifr = soup.css_first("iframe")

    if not ifr or not (src := ifr.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element found.")
        return nones

    ifr_src = network.ensure_https(src)

    if not (
        ifr_src_data := await network.request(
            ifr_src,
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        return nones

    valid_m3u8 = re.compile(
        r"(file|source|streamurls?)\s*(:|=)\s+(\'|\")([^\"]*)(\'|\")",
        re.I,
    )

    valid_m3u8_2 = re.compile(
        r"(streamurls|0x31c4)\s?=\s?\[\s*[\"']([^\"']+)[\"']",
        re.I,
    )

    if match := valid_m3u8.search(ifr_src_data.text):
        log.info(f"URL {url_num}) Captured M3U8")
        return json.loads(f'"{match[4]}"'), ifr_src

    elif match := valid_m3u8_2.search(ifr_src_data.text):
        log.info(f"URL {url_num}) Captured M3U8")
        return json.loads(f'"{match[2]}"'), ifr_src

    log.warning(f"URL {url_num}) No source found.")
    return nones


async def refresh_api_cache(now: Time) -> list[dict[str, Any]]:
    tasks = [
        network.request(
            urljoin(BASE_URL, "api/v1/games.php"),
            params={"sport": sport, "limit": 100},
            timeout=httpx.Timeout(25.0),
            log=log,
        )
        for sport in SPORT_ENDPOINTS
    ]

    results = await asyncio.gather(*tasks)

    if not (
        api_data := [*chain.from_iterable(r.json().get("data") for r in results if r)]
    ):
        return [{"timestamp": now.timestamp()}]

    api_data[-1]["timestamp"] = now.timestamp()

    return api_data


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    now = Time.rn()

    events: list[Event] = []

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")

        api_data = await refresh_api_cache(now)

        API_FILE.write(api_data)

    start_dt = now.delta(hours=-3)
    end_dt = now.delta(minutes=30)

    for stream_group in api_data:
        if not all(
            values := [
                stream_group.get(x)
                for x in (
                    "league",
                    "start_at",
                    "away",
                    "home",
                    "streams",
                )
            ]
        ):
            continue

        sport, event_time, away, home, streams = values

        sport = " ".join(i.strip() for i in sport.split("_"))

        event_dt = Time.fromisoformat(event_time).to_tz("EST")

        if not start_dt <= event_dt <= end_dt:
            continue

        elif not (home_team := home.get("name")):
            continue

        name = (
            f"{away_team} vs {home_team}"
            if (away_team := away.get("name")) and away_team != home_team
            else home_team
        )

        stream_urls: dict[str, str | None] = {
            stream.get("label") or "English": stream.get("url") for stream in streams
        }

        events.extend(
            Event(
                sport=sport,
                name=f"{name} | {lang}",
                link=urljoin(BASE_URL, url),
                timestamp=now.timestamp(),
            )
            for lang, url in stream_urls.items()
            if url
            if f"[{sport}] {name} | {lang} ({TAG})" not in cached_keys
            if lang.lower() != "hd"
        )

    return events


async def scrape() -> None:
    cached_sources = CACHE_FILE.load()

    valid_sources = {k: v for k, v in cached_sources.items() if v["source"]}

    valid_count = cached_count = len(valid_sources)

    urls.update(valid_sources)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_sources.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                url=ev.link,
                url_num=i,
            )

            source, iframe = await network.safe_process(
                handler,
                url_num=i,
                timeout_return=(None, None),
                semaphore=network.HTTP_S,
                log=log,
            )

            key = f"[{ev.sport}] {ev.name} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

            entry = {
                "source": source,
                "logo": logo,
                "refer": iframe,
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
                "link": ev.link,
            }

            cached_sources[key] = entry

            if source:
                valid_count += 1

                entry["source"] = clean_m3u(source)

                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_sources)
