import asyncio
import json
import re
from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from urllib.parse import urljoin

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "MP66"

CACHE_FILE = Cache(TAG, exp=10_800)

API_URLS = {
    sport: f"https://api.{sport.lower()}24all.ir"
    for sport in [
        "MLB",
        # "NBA",
        "NFL",
        # "NHL",
    ]
}

BASE_URLS = {sport: url.replace("api.", "") for sport, url in API_URLS.items()}


@dataclass(kw_only=True, slots=True)
class MPEvent(Event):
    link: str | None = None
    flavor_id: str
    media_id: int


async def process_event(
    sport: str,
    flavor_id: str,
    media_id: int,
    url_num: int,
) -> str | None:

    r = await network.client.post(
        urljoin(API_URLS[sport], "api/v2/generate_stream_info"),
        headers={"Referer": BASE_URLS[sport]},
        json={"flavor_id": flavor_id, "media_event_id": media_id},
    )

    if not r.is_success:
        log.warning(f"URL {url_num}) Failed to create post request.")
        return

    data: dict[str, str] = r.json()

    if not (m3u8_url := data.get("url")):
        log.warning(f"URL {url_num}) No M3U8 found")
        return

    log.info(f"URL {url_num}) Captured M3U8")

    return m3u8_url


async def get_events(cached_keys: KeysView[str]) -> list[MPEvent]:
    tasks = [network.request(url, log=log) for url in BASE_URLS.values()]

    results = await asyncio.gather(*tasks)

    events: list[MPEvent] = []

    if not (html_data := [(html.text, html.url) for html in results if html]):
        return events

    now = Time.rn()

    stateshot_ptrn = re.compile(r"var\s+stateshot\s+=\s+(.*);", re.I)

    start_dt = now.delta(hours=-1)
    end_dt = now.delta(minutes=1)

    for content, url in html_data:
        sport = next((k for k, v in BASE_URLS.items() if v == url), "Live Event")

        if not (match := stateshot_ptrn.search(content)):
            continue

        data: dict = json.loads(f"{match[1]}")

        teams, flavors, media_events = (
            data.get(x, {})
            for x in (
                "teams",
                "flavors",
                "media_events",
            )
        )

        team_identifier: dict[int, str] = {t.get("id"): t.get("name") for t in teams}

        event_to_flavor_id: dict[int, str] = {
            event_id: flavor["id"]
            for flavor in flavors
            for event_id in flavor.get("media_event_ids", [])
        }

        parsed_media_events: dict[int, int] = {
            x.get("game_id"): x.get("id") for x in media_events
        }

        for game in data.get("games", {}):
            game_id = game["id"]

            event_dt = Time.fromisoformat(game["datetime"]).to_tz("EST")

            if not start_dt <= event_dt <= end_dt:
                continue

            away = team_identifier.get(game["away_team_id"])
            home = team_identifier.get(game["home_team_id"])

            if f"[{sport}] {(event_name:=f"{away} vs {home}")} ({TAG})" in cached_keys:
                continue

            media_id = parsed_media_events.get(game_id, 0)

            if (flavor_id := event_to_flavor_id.get(media_id)) and (
                flavor_id.lower().startswith("free.live")
            ):
                events.append(
                    MPEvent(
                        sport=sport,
                        name=event_name,
                        timestamp=event_dt.timestamp(),
                        flavor_id=flavor_id,
                        media_id=media_id,
                    )
                )

    return events


async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["source"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info('Scraping from "https://mainportal66.com"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                sport=ev.sport,
                flavor_id=ev.flavor_id,
                media_id=ev.media_id,
                url_num=i,
            )

            source = await network.safe_process(
                handler,
                url_num=i,
                semaphore=network.HTTP_S,
                log=log,
            )

            key = f"[{ev.sport}] {ev.name} ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

            entry = {
                "source": source,
                "logo": logo,
                "refer": BASE_URLS[ev.sport],
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
            }

            cached_urls[key] = entry

            if source:
                valid_count += 1

                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
