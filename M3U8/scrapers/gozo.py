import re
from collections.abc import KeysView
from functools import partial
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "GOZO"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://gozowatch.top/updates"


def fix_name(n: str) -> str:
    return " ".join(
        (
            i.upper()
            if len(i) <= 3 and i.lower() != "vs"
            else i.capitalize().replace("Vs", "vs")
        )
        for i in n.split("-")
    )


async def process_event(url: str, url_num: int) -> str | None:
    if not (html_data := await network.request(url, url_num, log=log)):
        return

    ptrn = re.compile(r'var\s?sourceurl\s?=\s?"(.*)";', re.I)

    if not (match := ptrn.search(html_data.text)):
        log.warning(f"URL {url_num}) No source url found.")
        return

    log.info(f"URL {url_num}) Captured M3U8")

    return match[1]


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    events: list[Event] = []

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)

    if not (table := soup.css_first("table#table-content")):
        return events

    sport = "Live Event"

    for row in table.css("tr"):
        for game in (cell for cell in row.css("td > a") if cell):
            if not (href := game.attributes.get("href")) or href == "/":
                continue

            event_name = fix_name(game.text(strip=True))

            if f"[{sport}] {event_name} ({TAG})" in cached_keys:
                continue

            events.append(
                Event(
                    sport=sport,
                    name=event_name,
                    link=urljoin(BASE_URL, href),
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
                valid_count += 1

                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
