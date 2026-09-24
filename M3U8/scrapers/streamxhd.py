import ast
import base64
import re
from collections.abc import KeysView
from functools import partial
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "STRMXHD"

CACHE_FILE = Cache(TAG, exp=19_800)

BASE_URL = "https://streamxhd.com"


async def process_event(url: str, url_num: int) -> str | None:
    if not (
        event_data := await network.request(
            url,
            url_num,
            headers={"Referer": BASE_URL},
            log=log,
        )
    ):
        return

    digit_func_ptrn = re.compile(r"{return\s+(\d*);}", re.I)

    if not (digit_list := digit_func_ptrn.findall(event_data.text)):
        log.warning(f"URL {url_num}) Unable to decode url.")
        return

    embed_list_ptrn = re.compile(
        r"(\w+)\s*=\s*(\[\[.*?\]\])\s*;(?=\s*\1\.sort\()",
        re.S,
    )

    if not (match := embed_list_ptrn.search(event_data.text)):
        log.warning(f"URL {url_num}) Unable to decode url.")
        return

    embed_list_str = match[0].split("=", 1)[-1].strip(";")

    embed_list: list[tuple[int, str]] = ast.literal_eval(embed_list_str)

    m3u8 = "".join(
        chr(
            int("".join(c for c in base64.b64decode(v).decode("utf-8") if c.isdigit()))
            - sum(map(int, digit_list[:2]))
        )
        for _, v in sorted(embed_list, key=lambda i: i[0])
    )

    splits = urlsplit(m3u8)

    params = [(k, v) for k, v in parse_qsl(splits.query) if k.lower() != "ip"]

    log.info(f"URL {url_num}) Captured M3U8")

    return urlunsplit(splits._replace(query=urlencode(params)))


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    events: list[Event] = []

    if not (
        api_req := await network.request(
            urljoin(BASE_URL, "eventos.json"),
            log=log,
        )
    ):
        return events

    elif not (api_data := api_req.json()):
        return events

    now = Time.rn()

    for event in api_data.get("sports", []):
        if not (sport_leagues := event.get("leagues")):
            continue

        for league in sport_leagues:
            sport = league["name"]

            if not (league_events := league.get("events")):
                continue

            for event_info in league_events:
                name = event_info["title"]

                event_time = event_info["time"]

                event_dt = Time.from_str(event_time, tz_name="EST")

                if event_dt.date() != now.date():
                    continue

                elif not (event_servers := event_info.get("servers")):
                    continue

                event_links = {
                    x["url"]: x["name"] for x in event_servers if ".php" in x["url"]
                }

                events.extend(
                    Event(
                        sport=sport,
                        name=f"{name} | {lang}",
                        link=link,
                        timestamp=now.timestamp(),
                    )
                    for link, lang in event_links.items()
                    if f"[{sport}] {name} | {lang} ({TAG})" not in cached_keys
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
        log.info(f"Processing {len(events)} URL(s)")

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
                valid_count += 1

                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_urls)
