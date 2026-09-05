import json
import re
from collections.abc import KeysView
from functools import partial
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "STRMHUB"

CACHE_FILE = Cache(TAG, exp=10_800)

BASE_URL = "https://intelsportz.pro"


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (event_data := await network.request(url, url_num, log=log)):
        return nones

    soup_1 = HTMLParser(event_data.content)

    ifr_1 = soup_1.css_first("iframe#playerIframe")

    if not ifr_1 or not (ifr_1_src := ifr_1.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element/src found. (IFR1)")
        return nones

    elif not (ifr_1_src_data := await network.request(ifr_1_src, url_num, log=log)):
        return nones

    soup_2 = HTMLParser(ifr_1_src_data.content)

    ifr_2 = soup_2.css_first("iframe[allowfullscreen]")

    if not ifr_2 or not (ifr_2_src := ifr_2.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element/src found. (IFR2)")
        return nones

    if not (ifr_2_src_data := await network.request(ifr_2_src, url_num, log=log)):
        return nones

    ptrn = re.compile(r'\|\|\s?(".*");')

    if not (match := ptrn.search(ifr_2_src_data.text)):
        log.warning
        return nones

    log.info(f"URL {url_num}) Captured M3U8")

    return json.loads(match[1]), ifr_2_src


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    events: list[Event] = []

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)

    for card in soup.css(".live-card"):
        if not (href := card.attributes.get("href")):
            continue

        elif not (team_elems := card.css(".live-team-name")):
            continue

        sport = "".join(
            x for x in card.css_first(".live-league").text(strip=True) if x.isascii()
        ).lstrip()

        event_name = (
            "".join(x.text(strip=True) for x in team_elems)
            if len(team_elems) == 1
            else " vs ".join(x.text(strip=True) for x in team_elems)
        )

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
