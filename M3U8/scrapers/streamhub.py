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

CACHE_FILE = Cache(TAG, exp=16_200)

HTML_FILE = Cache(f"{TAG}-html", exp=28_800)

BASE_URL = "https://intelsportz.pro/"


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


async def refresh_html_cache(now: Time) -> dict[str, dict[str, str | float]]:
    events = {}

    if not (
        html_data := await network.request(
            BASE_URL,
            params={"date": f"{now:%Y-%m-%d}"},
            log=log,
        )
    ):
        return events

    soup = HTMLParser(html_data.content)

    for card in soup.css(".league-block"):
        if not (sport_elem := card.css_first(".league-name")):
            continue

        sport = sport_elem.text(strip=True)

        for match in card.css(".match-row"):
            if not (time_elem := match.css_first(".time > span.countdown")):
                continue

            elif not (event_ts := time_elem.attributes.get("data-start")):
                continue

            if not (watch_btn := match.css_first("a.watch-live")):
                continue

            elif not (href := watch_btn.attributes.get("href")):
                continue

            if not (team_elem := match.css(".team")):
                continue

            if match.css_first(".score").text(strip=True).lower() == "scheduled":
                event_name = team_elem[0].text(strip=True)

            else:
                event_name = " vs ".join(i.text(strip=True) for i in team_elem)

            key = f"[{sport}] {event_name} ({TAG})"

            events[key] = {
                "sport": sport,
                "name": event_name,
                "link": urljoin(BASE_URL, href),
                "event_ts": int(event_ts),
                "timestamp": now.timestamp(),
            }

    return events


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    now = Time.rn()

    if not (events := HTML_FILE.load()):
        log.info("Refreshing HTML cache")

        events = await refresh_html_cache(now)

        HTML_FILE.write(events)

    start_ts = now.delta(minutes=-30).timestamp()
    end_ts = now.delta(minutes=30).timestamp()

    return [
        Event(**v)
        for k, v in events.items()
        if k not in cached_keys and start_ts <= v["event_ts"] <= end_ts
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
                "timestamp": ev.event_ts,
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
