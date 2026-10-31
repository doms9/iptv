from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import urljoin

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "DAMITV"

CACHE_FILE = Cache(TAG, exp=10_800)

API_FILE = Cache(f"{TAG}-api", exp=19_800)

BASE_URL = "https://damitv.st"


@dataclass(kw_only=True, slots=True)
class DAMIEvent(Event):
    stream_id: str
    ppv_id: str
    link: str | None = None
    logo: str | None = None


async def process_event(
    ppv_id: str,
    stream_id: str,
    url_num: int,
) -> str | None:

    if not (
        event_data := await network.request(
            urljoin(BASE_URL, f"papi/extract-url/{ppv_id}"),
            url_num,
            headers={"Referer": urljoin(BASE_URL, f"embed/?id={stream_id}")},
            log=log,
        )
    ):
        return

    api_data = event_data.json() or {}

    if not (m3u8 := api_data.get("sdUrl") or api_data.get("hlsUrl")):
        log.warning(f"URL {url_num}) No source found.")
        return

    log.info(f"URL {url_num}) Captured M3U8")

    return m3u8


async def get_events(cached_keys: KeysView[str]) -> list[DAMIEvent]:
    now = Time.rn()

    events: list[DAMIEvent] = []

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")

        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(
            urljoin(BASE_URL, "papi/matches/all-today"),
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
                    "title",
                    "league",
                    "date",
                    "id",
                    "ppvId",
                )
            ]
        ):
            continue

        name, sport, start_ts, stream_id, ppv_id = values

        if stream_id.lower().startswith(("dl-", "247")):
            continue

        elif sport.startswith("24/7"):
            continue

        sport = (
            " ".join(i.capitalize().strip() for i in sport.split("-"))
            if sport.islower()
            else sport
        )

        event_dt = Time.from_ts(event_ts := int(f"{start_ts}"[:-3]))

        if not start_dt <= event_dt <= end_dt:
            continue

        elif f"[{sport}] {name} ({TAG})" in cached_keys:
            continue

        if poster := event.get("poster"):
            poster = poster if poster.startswith("http") else urljoin(BASE_URL, poster)

        events.append(
            DAMIEvent(
                sport=sport,
                name=name,
                logo=poster,
                stream_id=stream_id,
                ppv_id=ppv_id,
                timestamp=event_ts,
            )
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
                ppv_id=ev.ppv_id,
                stream_id=ev.stream_id,
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
                "logo": ev.logo or logo,
                "refer": urljoin(BASE_URL, f"embed/?id={ev.stream_id}"),
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
            }

            cached_sources[key] = entry

            if source:
                valid_count += 1

                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_sources)
