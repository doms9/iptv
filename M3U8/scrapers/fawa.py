import re
from functools import partial
from urllib.parse import quote, urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "FAWA"

CACHE_FILE = Cache(TAG, exp=14_400)

BASE_URL = "http://www.fawanews.sc/"


async def process_event(url: str, url_num: int) -> str | None:
    if not (
        html_data := await network.request(
            url,
            url_num,
            headers={"Referer": BASE_URL},
            log=log,
        )
    ):
        return

    elif f"{html_data.url}" in (BASE_URL, BASE_URL.rstrip("/")):
        log.info(f"URL {url_num}) No event found.")
        return

    valid_m3u8 = re.compile(r"var\s*\w*\s*=\s+\[(.*)];", re.I)

    if not (match := valid_m3u8.search(html_data.text)):
        log.warning(f"URL {url_num}) Unable to parse event source")
        return

    if source := "".join(
        re.search(rf'var\s*{var}\s*=\s*"(.*)";', html_data.text, re.I)[1]
        for var in (i.strip() for i in match[1].split("+"))
    ):
        log.info(f"URL {url_num}) Captured M3U8")
        return source.strip()

    log.warning(f"URL {url_num}) No M3U8 found")


async def get_events(cached_links: set[str]) -> list[Event]:
    events: list[Event] = []

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)

    valid_event = re.compile(r"\d{1,2}:\d{1,2}")
    clean_event = re.compile(r"\s+-+\s+\w{1,4}")

    for item in soup.css(".user-item"):
        text_elem = item.css_first(".user-item__name")
        subtext_elem = item.css_first(".user-item__playing")
        link_elem = item.css_first("a[href]")

        if not (text_elem and subtext_elem):
            continue

        elif not (href := link_elem.attributes.get("href")):
            continue

        elif (link := urljoin(f"{html_data.url}", quote(href))) in cached_links:
            continue

        event_name, details = text_elem.text(strip=True), subtext_elem.text(strip=True)

        if not (valid_event.search(details)):
            continue

        sport = valid_event.split(details)[0].strip()

        events.append(
            Event(
                sport=sport,
                name=clean_event.sub("", event_name),
                link=link,
            )
        )

    return events


async def scrape() -> None:
    cached_sources = CACHE_FILE.load()

    cached_links = {entry["link"] for entry in cached_sources.values()}

    valid_sources = {k: v for k, v in cached_sources.items() if v["source"]}

    valid_count = cached_count = len(valid_sources)

    urls.update(valid_sources)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_links):
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
                "refer": BASE_URL,
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
                "link": ev.link,
            }

            cached_sources[key] = entry

            if source:
                valid_count += 1

                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_sources)
