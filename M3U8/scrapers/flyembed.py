import json
import re
from collections.abc import KeysView
from functools import partial

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "FLYEMBD"

CACHE_FILE = Cache(TAG, exp=7_200)

API_FILE = Cache(f"{TAG}-api", exp=19_800)


def clean_name(s: str) -> str:
    return re.sub(r"(\r|\n)", "", s).strip()


# def clean_m3u(s: str) -> str:
#     return re.sub(r"\.live\n", ".pro", s)


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (html_data := await network.request(url, url_num, log=log)):
        return nones

    soup = HTMLParser(html_data.content)

    iframe = soup.css_first("iframe")

    if not iframe or not (iframe_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe source found.")
        return nones

    elif not (
        iframe_src_data := await network.request(
            iframe_src,
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        return nones

    num_list_ptrn = re.compile(r"var\s+_(\w+)=\[([^\]]*)\],", re.S)

    index_ptrn = re.compile(r"(_[a-z]+\d+)=(\d+)")

    m3u_ptrn = re.compile(r'(var\s?signed_)?url\s?=\s?"(.*)";', re.I)

    # z_ptrn = re.compile(r"\&(\d+)")

    # if not (z_mtch := z_ptrn.search(html_data.text)):
    #     log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
    #     return nones

    if not (num_list_mtch := num_list_ptrn.findall(iframe_src_data.text)):
        log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
        return nones

    elif not (index_mtch := index_ptrn.findall(iframe_src_data.text)):
        log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
        return nones

    num_list = (int(n.strip()) for n in num_list_mtch[-1][-1].split(","))

    if len(index_mtch) > 2:
        index_mtch.pop()

    x, y = (int(i[-1].strip()) for i in index_mtch)

    # z = int(z_mtch[1])

    js = "".join(chr(((i ^ x) - y + 256) & 255) for i in num_list)

    if not (m3u_mtch := m3u_ptrn.search(js)):
        log.warning(f"URL {url_num}) No M3U8 source found.")
        return nones

    log.info(f"URL {url_num}) Captured M3U8")

    return json.loads(f'"{m3u_mtch[2]}"'), iframe_src


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    now = Time.rn()

    events: list[Event] = []

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")

        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(
            "https://ovogoal.cyou/api/v2/flyembed.json",
            log=log,
        ):
            api_data: list[dict[str, str]] = r.json()

            api_data[-1]["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    start_dt = now.delta(hours=-3)
    end_dt = now.delta(minutes=30)

    for event_group in api_data:
        if not all(
            values := [
                event_group.get(x)
                for x in (
                    "League",
                    "Team 1 ",
                    "Team2",
                    "Date",
                    "Time",
                    "iframeURL",
                )
            ]
        ):
            continue

        sport, away, home, date, time, link = values

        event_dt = Time.from_str(f"{date.replace(' ','')} {time}", tz_name="GMT")

        if not start_dt <= event_dt <= end_dt:
            continue

        sport, name = clean_name(sport), clean_name(f"{away} vs {home}")

        if f"[{sport}] {name} ({TAG})" in cached_keys:
            continue

        events.append(
            Event(
                sport=sport,
                name=name,
                link=link,
                timestamp=now.timestamp(),
            )
        )

    return events


async def scrape() -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["source"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info('Scraping from "https://flyembed.xyz"')

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
                "timestamp": ev.timestamp,
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
