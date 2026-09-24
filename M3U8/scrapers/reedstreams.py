from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "REED"

CACHE_FILE = Cache(TAG, exp=10_800)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_DOMAIN = "reedstreams.link"


@dataclass(kw_only=True, slots=True)
class REEDEvent(Event):
    logo: str | None = None


async def process_event(url: str, url_num: int) -> str | None:
    if not (event_data := await network.request(url, url_num, log=log)):
        return

    elif not (streams := event_data.json().get("streams")):
        log.warning(f"URL {url_num}) No streams available")
        return

    stream_urls: list[str | None] = [
        stream.get("embedUrl") for stream in streams if stream.get("source") == "tnasty"
    ]

    if not stream_urls:
        log.warning(f"URL {url_num}) No valid stream url found")
        return

    stream_url = stream_urls[0]

    if not (m3u := dict(parse_qsl(urlsplit(stream_url).query)).get("url")):
        log.warning(f"URL {url_num}) Failed to parse url")
        return

    log.info(f"URL {url_num}) Captured M3U8")

    return unquote(m3u)


async def get_events(cached_keys: KeysView[str]) -> list[REEDEvent]:
    now = Time.rn()

    events: list[REEDEvent] = []

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")

        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(
            urljoin(f"https://api.{BASE_DOMAIN}", "api/matches/all"),
            log=log,
        ):
            api_data: list[dict[str, Any]] = r.json()

            api_data[-1]["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    start_dt = now.delta(minutes=-30)
    end_dt = now.delta(minutes=30)

    for event in api_data:
        if not all(
            values := [
                event.get(x)
                for x in (
                    "category",
                    "title",
                    "league_name",
                    "date",
                    "id",
                )
            ]
        ):
            continue

        category, name, sport, start_ts, stream_id = values

        if category not in {
            "american-football",
            "baseball",
            # "basketball",
            "hockey",
        }:
            continue

        event_dt = Time.from_ts(event_ts := int(f"{start_ts}"[:-3]))

        if not start_dt <= event_dt <= end_dt:
            continue

        elif f"[{sport}] {name} ({TAG})" in cached_keys:
            continue

        logo = (
            urljoin(f"https://api.{BASE_DOMAIN}", poster)
            if (poster := event.get("poster"))
            else None
        )

        events.append(
            REEDEvent(
                sport=sport,
                name=name,
                logo=logo,
                link=urljoin(f"https://links.{BASE_DOMAIN}", f"stream/{stream_id}"),
                timestamp=event_ts,
            )
        )

    return events


async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["source"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    base_url = network.ensure_https(f"//{BASE_DOMAIN}")

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{base_url}"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                url=ev.link,
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
                "refer": base_url,
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
