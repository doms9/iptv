import base64
import json
import re
from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "EMBEDSPRT"

CACHE_FILE = Cache(TAG, exp=10_800)

HTML_FILE = Cache(f"{TAG}-html", exp=28_800)

BASE_URL = "https://embedsport.live/"


@dataclass(kw_only=True, slots=True)
class EMBDEvent(Event):
    link: str | None = None
    event_id: str
    event_ts: int | float


async def process_event(url_num: int, event_id: str) -> str | None:
    if not (
        event_data := await network.request(
            "https://embedfootball.site/ppv/",
            url_num,
            params={"id": event_id},
            headers={"Referer": BASE_URL},
            log=log,
        )
    ):
        return

    pattern = re.compile(r'const\s+stream\s+=\s+"([^"]*)"', re.I)

    if not (match := pattern.search(event_data.text)):
        log.warning(f"URL {url_num}) No source found.")
        return

    log.info(f"URL {url_num}) Captured M3U8")

    return match[1]


async def refresh_html_cache(now: Time) -> dict[str, dict[str, str | float]]:
    events = {}

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)

    for card in soup.css(".stream-card"):
        if not all(
            values := [
                card.attributes.get(x)
                for x in (
                    "data-category",
                    "data-start",
                    "data-servers",
                )
            ]
        ):
            continue

        sport, event_ts, event_enc = values

        if sport.lower() == "24/7 streams":
            continue

        event_dt = Time.from_ts(int(event_ts))

        if event_dt.date() != now.date():
            continue

        name = card.css_first(".leading-tight").text(strip=True)

        key = f"[{sport}] {name} ({TAG})"

        event_info: list[dict[str, str]] = json.loads(
            base64.b64decode(event_enc).decode("utf-8")
        )

        if not (event_id := event_info[0].get("encoded_id")):
            continue

        events[key] = {
            "sport": sport,
            "name": name,
            "event_id": event_id,
            "event_ts": event_dt.timestamp(),
            "timestamp": now.timestamp(),
        }

    return events


async def get_events(cached_keys: KeysView[str]) -> list[EMBDEvent]:
    now = Time.rn()

    if not (events := HTML_FILE.load()):
        log.info("Refreshing HTML cache")

        events = await refresh_html_cache(now)

        HTML_FILE.write(events)

    start_ts = now.delta(hours=-3).timestamp()
    end_ts = now.delta(minutes=30).timestamp()

    return [
        EMBDEvent(**v)
        for k, v in events.items()
        if k not in cached_keys
        and (start_ts <= (event_ts := v["event_ts"]) <= end_ts or event_ts == 0)
    ]


async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["source"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                url_num=i,
                event_id=ev.event_id,
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
                "refer": BASE_URL,
                "timestamp": ev.event_ts,
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
