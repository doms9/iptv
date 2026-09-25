import re
from collections import defaultdict
from functools import partial
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "PLIBRE"

CACHE_FILE = Cache(TAG, exp=19_800)

BASE_URL = "https://la18hd.su"


async def process_event(url: str, url_num: int) -> str | None:
    if not (html_data := await network.request(url, url_num, log=log)):
        return

    valid_m3u8 = re.compile(r'var\s+playbackURL\s+=\s+"([^"]*)"', re.I)

    if not (match := valid_m3u8.search(html_data.text)):
        log.warning(f"URL {url_num}) No M3U8 found")
        return

    splits = urlsplit(match[1])

    params = [(k, v) for k, v in parse_qsl(splits.query) if k.lower() != "ip"]

    log.info(f"URL {url_num}) Captured M3U8")

    return urlunsplit(splits._replace(query=urlencode(params)))


async def get_events() -> list[Event]:
    events: list[Event] = []

    if not (
        api_req := await network.request(
            urljoin(BASE_URL, "eventos/json/agenda123.json"),
            log=log,
        )
    ):
        return events

    api_data: list[dict[str, str]] = api_req.json()

    counter: dict[str, int] = defaultdict(int)

    for event_info in api_data:
        if not all(
            values := [
                event_info.get(x)
                for x in (
                    "title",
                    "category",
                    "link",
                )
            ]
        ):
            continue

        title, sport, link = values

        if len(title_splits := title.split(":", 1)) > 1:
            sport, title = (i.strip() for i in title_splits[:2])

        if not (url_splits := urlsplit(link)).query:
            continue

        elif not dict(parse_qsl(url_splits.query)).get("stream"):
            continue

        if not link.startswith(BASE_URL):
            link: str = urlunsplit(url_splits._replace(netloc=BASE_URL.split("/")[-1]))

        name = (
            f"{title.split("|")[0].strip()} | {lang}"
            if (lang := event_info.get("language", "").capitalize())
            else f"{title.split("|")[0].strip()}"
        )

        counter[name] += 1

        events.append(
            Event(
                sport=sport,
                name=f"{name} {counter[name]}",
                link=link.strip(),
            )
        )

    return events


async def scrape() -> None:
    if cached_urls := CACHE_FILE.load():
        urls.update({k: v for k, v in cached_urls.items() if v["source"]})

        log.info(f"Loaded {len(urls)} event(s) from cache")

        return

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events():
        log.info(f"Processing {len(events)} URL(s)")

        now = Time.rn()

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
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
            }

            cached_urls[key] = entry

            if source:
                urls[key] = entry

        log.info(f"Collected and cached {len(urls)} event(s)")

    else:
        log.info("No events found")

    CACHE_FILE.write(cached_urls)
