import ast
import base64
import re
from collections import defaultdict
from functools import partial
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "STP"

CACHE_FILE = Cache(TAG, exp=19_800)

BASE_URL = "https://streamx305.sbs"


async def process_event(url: str, url_num: int) -> str | None:
    if not (
        event_data := await network.request(
            url,
            url_num,
            headers={
                "Referer": BASE_URL,
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
            },
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


async def get_events() -> list[Event]:
    now = Time.rn()

    events: list[Event] = []

    if not (
        api_req := await network.request(
            urljoin(BASE_URL, "json/agenda550.json"),
            log=log,
        )
    ):
        return events

    counter: dict[str, int] = defaultdict(int)

    api_data: list[dict[str, str]] = api_req.json()

    sport = "Live Event"

    for event_info in api_data:
        if not all(
            values := [
                event_info.get(x)
                for x in (
                    "title",
                    "link",
                    "date",
                )
            ]
        ):
            continue

        title, link, event_date = values

        if event_date != f"{now.date()}":
            continue

        if len(title_splits := title.split(":", 1)) > 1:
            sport, title = (i.strip() for i in title_splits[:2])

        # if not (url_splits := urlsplit(link)).query:
        #     continue

        # elif not dict(parse_qsl(url_splits.query)).get("stream"):
        #     continue

        name = (
            f"{title.split("|")[0].strip()} | {lang}"
            if (lang := event_info.get("language", "").capitalize())
            else f"{title.split("|")[0].strip()}"
        )

        counter[name] += 1

        events.append(
            Event(
                sport=sport,
                name=f"{name} {counter[name]}",
                link=link,
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
                urls[key] = entry

        log.info(f"Collected and cached {len(urls)} event(s)")

    else:
        log.info("No events found")

    CACHE_FILE.write(cached_urls)
