import re
from functools import partial
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "OVO"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://ovostream.net/"


def fix_sport(s: str) -> str:
    return s.upper() if len(s) <= 4 else s.capitalize()


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (html_data := await network.request(url, url_num, log=log)):
        return nones

    soup = HTMLParser(html_data.content)

    ifr = soup.css_first(".player-box > iframe")

    if not ifr or not (src := ifr.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element found")
        return nones

    elif not (
        ifr_src_data := await network.request(
            src,
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        return nones

    ptrn = re.compile(r'var\s?sourceurl\s?=\s?"(.*)";', re.I)

    if not (match := ptrn.search(ifr_src_data.text)):
        log.warning(f"URL {url_num}) No source url found.")
        return nones

    log.info(f"URL {url_num}) Captured M3U8")

    return match[1], src


async def get_events() -> list[Event]:
    events: list[Event] = []

    if not (html_data := await network.request(f"{BASE_URL}?", log=log)):
        return events

    soup = HTMLParser(html_data.content)

    for card in soup.css(".card"):
        if not (watch_btn := card.css_first("a.watch-btn")):
            continue

        elif not (href := watch_btn.attributes.get("href")):
            continue

        if not (teams := [i.text(strip=True) for i in card.css(".team-name") if i]):
            continue

        sport = fix_sport(card.css_first(".sport-tag").text(strip=True))

        event_name = (
            teams[0] if len(teams) == 1 else " vs ".join(i.strip() for i in teams)
        )

        events.append(
            Event(
                sport=sport,
                name=event_name,
                link=urljoin(BASE_URL, href),
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
            }

            cached_urls[key] = entry

            if source:
                urls[key] = entry

        log.info(f"Collected and cached {len(urls)} event(s)")

    else:
        log.info("No events found")

    CACHE_FILE.write(cached_urls)
