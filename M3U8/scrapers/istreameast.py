import re
from collections.abc import KeysView
from functools import partial

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "iSTRMEAST"

CACHE_FILE = Cache(TAG, exp=10_800)

BASE_URL = "https://thestreameast.one"


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (event_data := await network.request(url, url_num, log=log)):
        return nones

    soup = HTMLParser(event_data.content)

    if not (iframe := soup.css_first("iframe#wp_player")):
        log.warning(f"URL {url_num}) No iframe element found.")
        return nones

    elif not (iframe_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe source found.")
        return nones

    elif not (iframe_src_data := await network.request(iframe_src, url_num, log=log)):
        return nones

    pattern = re.compile(r'const\s+source\s+=\s+"([^"]*)"', re.I)

    if not (match := pattern.search(iframe_src_data.text)):
        log.warning(f"URL {url_num}) No Clappr source found.")
        return nones

    log.info(f"URL {url_num}) Captured M3U8")

    return match[1], iframe_src


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    events: list[Event] = []

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)

    for link in soup.css("li.f1-podium--item > a.f1-podium--link"):
        if not (li_item := link.parent):
            continue

        elif not all(
            values := [
                li_item.css_first(x)
                for x in (
                    ".f1-podium--rank",
                    ".SaatZamanBilgisi",
                    ".f1-podium--driver",
                )
            ]
        ):
            continue

        rank_elem, time_elem, driver_elem = values

        if time_elem.text(strip=True).lower() != "live":
            continue

        sport = rank_elem.text(strip=True)

        event_name = driver_elem.text(strip=True)

        if inner_span := driver_elem.css_first("span.d-md-inline"):
            event_name = inner_span.text(strip=True)

        if f"[{sport}] {event_name} ({TAG})" in cached_keys:
            continue

        if not (href := link.attributes.get("href")):
            continue

        events.append(
            Event(
                sport=sport,
                name=event_name,
                link=href,
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
                "timestamp": now.timestamp(),
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
