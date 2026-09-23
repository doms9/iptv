import base64
import re
from functools import partial
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "TVF90"

CACHE_FILE = Cache(TAG, exp=19_800)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

API_URL = "https://api.wqxag.com/diaries.json"

BASE_URL = "https://tvf90.com"


async def process_event(url: str, url_num: int) -> str | None:
    if not (html_data := await network.request(url, url_num, log=log)):
        return

    soup = HTMLParser(html_data.content)

    iframe = soup.css_first("iframe#player-frame")

    if not iframe or not (ifr_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe source found.")
        return

    elif not (
        ifr_src_data := await network.request(
            urljoin(BASE_URL, ifr_src) if ifr_src.startswith("/") else ifr_src,
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        return

    valid_m3u8 = re.compile(r'var\s+playbackURL\s+=\s+"([^"]*)"', re.I)

    if not (match := valid_m3u8.search(ifr_src_data.text)):
        log.warning(f"URL {url_num}) No M3U8 found")
        return

    splits = urlsplit(match[1])

    params = [(k, v) for k, v in parse_qsl(splits.query) if k.lower() != "ip"]

    log.info(f"URL {url_num}) Captured M3U8")

    return urlunsplit(splits._replace(query=urlencode(params)))


async def get_events() -> list[Event]:
    now = Time.rn()

    events: list[Event] = []

    if not (api_data := API_FILE.load(per_entry=False)):
        log.info("Refreshing API cache")

        api_data = {"timestamp": now.timestamp()}

        if r := await network.request(API_URL, log=log):
            api_data: dict[str, list[dict[str, Any]]] = r.json()

            api_data["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    for event_info in api_data.get("data", []):
        if not (stream_attrs := event_info.get("attributes")):
            continue

        elif not (stream_embeds := stream_attrs.get("embeds", {}).get("data")):
            continue

        event_name = stream_attrs["diary_description"]

        event_date = stream_attrs["date_diary"]

        if event_date != f"{now.date()}":
            continue

        try:
            sport, name = (
                i.strip()
                for i in re.split(
                    r"[:–-]",
                    event_name,
                    maxsplit=1,
                )
            )
        except ValueError:
            sport, name = "Live Event", event_name

        for frames in stream_embeds:
            if not (frames_attrs := frames.get("attributes")):
                continue

            elif not (ifr_href := frames_attrs.get("embed_iframe")):
                continue

            splits = urlsplit(ifr_href)

            if not (b64_link := dict(parse_qsl(splits.query)).get("r")):
                continue

            events.append(
                Event(
                    sport=sport,
                    name=f"{name} | {frames_attrs["embed_name"]}",
                    link=base64.b64decode(b64_link).decode("utf-8").strip(),
                    timestamp=now.timestamp(),
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

            cached_urls[key] = entry

            if source:
                urls[key] = entry

        log.info(f"Collected and cached {len(urls)} event(s)")

    else:
        log.info("No events found")

    CACHE_FILE.write(cached_urls)
