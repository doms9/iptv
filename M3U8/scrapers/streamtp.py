import re
from collections import defaultdict
from functools import partial
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "STP"

CACHE_FILE = Cache(TAG, exp=19_800)

BASE_URL = "https://streamx723.sbs"


async def process_event(url: str, url_num: int) -> str | None:
    if not (
        event_data := await network.request(
            url,
            url_num,
            headers={
                "Referer": BASE_URL,
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
            },
            log=log,
        )
    ):
        return

    valid_m3u8 = re.compile(r'"src"\s?:"([^"]*)"', re.I)

    if not (match := valid_m3u8.search(event_data.text)):
        log.warning(f"URL {url_num}) No M3U8 found")
        return

    splits = urlsplit(match[1])

    params = [(k, v) for k, v in parse_qsl(splits.query) if k.lower() != "ip"]

    log.info(f"URL {url_num}) Captured M3U8")

    return urlunsplit(splits._replace(query=urlencode(params)))


async def get_events() -> list[Event]:
    now = Time.rn()

    events: list[Event] = []

    if not (
        api_req := await network.request(
            urljoin(BASE_URL, "json/agenda888.json"),
            log=log,
        )
    ):
        return events

    counter: dict[str, int] = defaultdict(int)

    api_data: list[dict[str, str]] = api_req.json()

    for event_info in api_data:
        if not all(
            values := [
                event_info.get(x)
                for x in (
                    "title",
                    "link",
                    "date",
                )
            ]
        ):
            continue

        title, link, event_date = values

        if event_date != f"{now.date()}":
            continue

        try:
            sport, name = (
                i.strip()
                for i in re.split(
                    r"[:–-]",
                    title,
                    maxsplit=1,
                )
            )
        except ValueError:
            sport, name = "Live Event", title

        # if not (url_splits := urlsplit(link)).query:
        #     continue

        # elif not dict(parse_qsl(url_splits.query)).get("stream"):
        #     continue

        name = (
            f"{name.split("|")[0].strip()} | {lang}"
            if (lang := event_info.get("language", "").capitalize())
            else f"{name.split("|")[0].strip()}"
        )

        counter[name] += 1

        events.append(
            Event(
                sport=sport,
                name=f"{name} {counter[name]}",
                link=link,
                timestamp=now.timestamp(),
            )
        )

    return events


async def scrape() -> None:
    if cached_sources := CACHE_FILE.load():
        urls.update({k: v for k, v in cached_sources.items() if v["source"]})

        log.info(f"Loaded {len(urls)} event(s) from cache")

        return

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events():
        log.info(f"Processing {len(events)} URL(s)")

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
                "refer": ev.link,
                "timestamp": ev.timestamp,
                "tvg-id": tvg_id or "Live.Event.us",
            }

            cached_sources[key] = entry

            if source:
                urls[key] = entry

        log.info(f"Collected and cached {len(urls)} event(s)")

    else:
        log.info("No events found")

    CACHE_FILE.write(cached_sources)
