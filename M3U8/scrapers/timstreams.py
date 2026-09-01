import json
import re
from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import urljoin

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "TIMSTRM"

CACHE_FILE = Cache(TAG, exp=7_200)

API_FILE = Cache(f"{TAG}-api", exp=19_800)

BASE_URL = "https://timstreams.st"


@dataclass(kw_only=True, slots=True)
class TIMEvent(Event):
    logo: str | None = None


async def process_event(url: str, url_num: int) -> str | None:
    if not (html_data := await network.request(url, url_num, log=log)):
        return

    num_list_ptrn = re.compile(r"var\s+_(\w+)=\[([^\]]*)\],", re.S)

    index_ptrn = re.compile(r"(_[a-z]+\d+)=(\d+)")

    m3u_ptrn = re.compile(r'(var\s?signed_)?url\s?=\s?"(.*)";', re.I)

    # z_ptrn = re.compile(r"\&(\d+)")
    # if not (z_mtch := z_ptrn.search(html_data.text)):
    #     log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
    #     return

    if not (num_list_mtch := num_list_ptrn.findall(html_data.text)):
        log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
        return

    elif not (index_mtch := index_ptrn.findall(html_data.text)):
        log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
        return

    num_list = (int(n.strip()) for n in num_list_mtch[-1][-1].split(","))

    if len(index_mtch) > 2:
        index_mtch.pop()

    x, y = (int(i[-1].strip()) for i in index_mtch)

    # z = int(z_mtch[1])

    js = "".join(chr(((i ^ x) - y + 256) & 255) for i in num_list)

    if not (m3u_mtch := m3u_ptrn.search(js)):
        log.warning(f"URL {url_num}) No M3U8 source found.")
        return

    log.info(f"URL {url_num}) Captured M3U8")

    return json.loads(f'"{m3u_mtch[2]}"')


async def get_events(cached_keys: KeysView[str]) -> list[TIMEvent]:
    now = Time.rn()

    events: list[TIMEvent] = []

    if not (api_data := API_FILE.load(per_entry=False)):
        log.info("Refreshing API cache")

        api_data = {"timestamp": now.timestamp()}

        if r := await network.request(
            urljoin(BASE_URL, "api/live-upcoming"),
            log=log,
        ):
            api_data: dict[str, list[dict[str, Any]]] = r.json()

            api_data["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    start_dt = now.delta(hours=-3)
    end_dt = now.delta(minutes=30)

    sport_genres = {}

    if genres := api_data.get("genres", []):
        sport_genres = {
            genre["id"]: {
                **{0: genre["name"]},
                **{sub["id"]: sub["name"] for sub in genre.get("sub_categories", [])},
            }
            for genre in genres
        }

    for event in api_data.get("events") or []:
        if not all(
            values := [
                event.get(x)
                for x in (
                    "name",
                    "genre",
                    "sub_genre",
                    "time",
                    "streams",
                )
            ]
        ):
            continue

        name, genre, sub_genre, event_time, streams = values

        if 17 <= genre <= 18:
            continue

        event_dt = Time.from_str(event_time, tz_name="EST")

        sport = sport_genres.get(genre, {}).get(sub_genre, "Live Event")

        if f"[{sport}] {name} ({TAG})" in cached_keys:
            continue

        elif not start_dt <= event_dt <= end_dt:
            continue

        elif not (stream_url := streams[0].get("url")):
            continue

        events.append(
            TIMEvent(
                sport=sport,
                name=name,
                link=stream_url,
                logo=event.get("logo"),
                timestamp=now.timestamp(),
            )
        )

    return events


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
                "logo": ev.logo or logo,
                "refer": ev.link,
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
