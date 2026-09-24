import ast
import asyncio
import re
from functools import partial
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "WEBCAST"

CACHE_FILE = Cache(TAG, exp=12_600)

BASE_URLS = {
    "MLB": {"base": "https://mlbwebcast.com", "api": "stream/check_stream.php"},
    "NFL": {"base": "https://nflwebcast.com", "api": "live/check_stream.php"},
    "NHL": {"base": "https://slapstreams.com", "api": "stream/check_stream.php"},
}


def fix_event(s: str) -> str:
    return " vs ".join(s.split("@"))


async def process_event(
    url: str,
    url_num: int,
    sport: str,
) -> str | None:

    if not (event_data := await network.request(url, url_num, log=log)):
        return

    soup = HTMLParser(event_data.content)

    if not (iframe := soup.css_first('iframe[name="srcFrame"]')):
        log.warning(f"URL {url_num}) No iframe element found.")
        return

    if not (iframe_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe source found.")
        return

    elif iframe_src.lower() == "about:blank":
        iframe_src = iframe.attributes.get("data-litespeed-src")

    if not (
        iframe_src_data := await network.request(
            iframe_src,
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        return

    pattern = re.compile(r'var\s+\w*=\[([^"]*)\];', re.I)

    if not (match := pattern.search(iframe_src_data.text)):
        log.warning(f"URL {url_num}) No Clappr source found.")
        return

    try:
        ev_id, ev_ts, ev_pt = ast.literal_eval(match[1])
    except ValueError:
        log.warning(f"URL {url_num}) Failed to parse event info.")
        return

    params: dict[str, int | str] = dict(zip(["id", "ts", "pt"], [ev_id, ev_ts, ev_pt]))

    if not (
        api_data := await network.request(
            urljoin(BASE_URLS[sport]["base"], BASE_URLS[sport]["api"]),
            url_num,
            headers={"Referer": iframe_src},
            params=params,
            log=log,
        )
    ):
        return

    elif (data := api_data.json()).get("error"):
        log.warning(f"URL {url_num}) Failed to make php request.")
        return

    elif not (m3u8 := data.get("url")):
        log.warning(f"URL {url_num}) No M3U8 found.")

    log.info(f"URL {url_num}) Captured M3U8")

    return m3u8


async def get_events() -> list[Event]:
    tasks = [
        network.request(url, log=log) for url in (i["base"] for i in BASE_URLS.values())
    ]

    results = await asyncio.gather(*tasks)

    events: list[Event] = []

    if not (
        soups := [(HTMLParser(html.content), html.url) for html in results if html]
    ):
        return events

    for soup, url in soups:
        sport = next(
            (k for k, v in BASE_URLS.items() if f"{url}".startswith(v["base"])),
            "Live Event",
        )

        for row in soup.css("tr.singele_match_date"):
            if not (vs_node := row.css_first("td.teamvs a")):
                continue

            event_name = vs_node.text(strip=True)

            for span in vs_node.css("span.mtdate"):
                date = span.text(strip=True)

                event_name = event_name.replace(date, "").strip()

            if not (href := vs_node.attributes.get("href")):
                continue

            events.append(
                Event(
                    sport=sport,
                    name=fix_event(event_name),
                    link=href,
                )
            )

    return events


async def scrape() -> None:
    if cached_urls := CACHE_FILE.load():
        urls.update({k: v for k, v in cached_urls.items() if v["source"]})

        log.info(f"Loaded {len(urls)} event(s) from cache")

        return

    log.info(f'Scraping from "{' & '.join(i["base"] for i in BASE_URLS.values())}"')

    if events := await get_events():
        log.info(f"Processing {len(events)} URL(s)")

        now = Time.rn()

        for i, ev in enumerate(events, start=1):
            handler = partial(
                process_event,
                url=ev.link,
                url_num=i,
                sport=ev.sport,
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
                "refer": BASE_URLS[ev.sport]["base"],
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
                "link": ev.link,
            }

            cached_urls[key] = entry

            if source:
                urls[key] = entry

        log.info(f"Collected and cached {len(urls)} event(s)")

    else:
        log.info("No events found")

    CACHE_FILE.write(cached_urls)
