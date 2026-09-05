from collections.abc import KeysView
from functools import partial
from urllib.parse import parse_qsl, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "STRMCNTR"

CACHE_FILE = Cache(TAG, exp=16_200)

HTML_FILE = Cache(f"{TAG}-html", exp=28_800)

BASE_URL = "https://streamecenter.live"

ALT_BASE = "https://streame.center"


def cleanup(s: str) -> str:
    return "".join(i for i in s.split("—")[-1] if i.isascii()).strip()


def fix_sport(s: str) -> str:
    splits = s.split()

    i = splits[0]

    return f"{i.upper() if len(i) < 5 else i.capitalize()} {' '.join(x.capitalize() for x in splits[1:])}".strip()


async def process_event(url: str, url_num: int) -> str | None:
    if not (html_data := await network.request(url, url_num, log=log)):
        return

    soup = HTMLParser(html_data.content)

    iframe = soup.css_first("iframe")

    if not iframe or not (src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element found.")
        return

    splits = urlsplit(src)

    params = dict(parse_qsl(splits.query))

    if not (stream_id := params.get("stream")):
        log.warning(f"URL {url_num}) No stream ID found.")
        return

    log.info(f"URL {url_num}) Captured M3U8")

    return (
        f"https://edgestream3.pro/stream/{stream_id}.m3u8"
        if stream_id.isdigit()
        else f"https://edgestream2.pro/hls/{stream_id}.m3u8"
    )


async def refresh_html_cache(now: Time) -> dict[str, dict[str, str | float]]:
    events = {}

    if not (
        html_data := await network.request(
            urljoin(BASE_URL, "game-cards/embed"),
            log=log,
        )
    ):
        return events

    soup = HTMLParser(html_data.content)

    for card in soup.css(".game-card-group"):
        if not (sport_elem := card.css_first("h2")):
            continue

        sport = fix_sport(cleanup(sport_elem.text(strip=True)))

        for game in card.css(".game-card-row"):
            if not (event_time_elem := game.css_first(".game-card-when > time")):
                continue

            elif not (event_time := event_time_elem.attributes.get("datetime")):
                continue

            event_dt = Time.fromisoformat(event_time).to_tz("EST")

            if event_dt.date() != now.date():
                continue

            if not (name_elem := game.css_first("h3")):
                continue

            elif not (event_name := name_elem.attributes.get("aria-label")):
                continue

            for source in game.css(".game-card-source > a.game-card-open-link"):
                if not (href := source.attributes.get("href")):
                    continue

                name = f"{event_name} | {source.text(strip=True)}"

                key = f"[{sport}] {name} ({TAG})"

                events[key] = {
                    "sport": sport,
                    "name": name,
                    "link": urljoin(BASE_URL, href),
                    "event_ts": event_dt.timestamp(),
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
                "refer": ALT_BASE,
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
