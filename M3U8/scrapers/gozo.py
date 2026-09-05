import ast
import base64
import re
from collections.abc import KeysView
from functools import partial
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "GOZO"

CACHE_FILE = Cache(TAG, exp=28_800)

BASE_URL = "https://gozo.st/"


def rot13(c: str) -> str:
    code = ord(c)
    base = 90 if c <= "Z" else 122
    return chr(code + 13 if base >= (code + 13) else code - 13)


def decrypt(enc: str, xor_key: int, num_list: list[int]) -> str | None:
    hex_enc_list = []

    rot13_list = []

    try:
        chars = list(enc)

        unshuffled = [""] * len(chars)

        for i, num in enumerate(num_list):
            unshuffled[num] = chars[i]

        xor_enc = "".join(unshuffled)

        hex_enc_list.extend(
            chr(int(xor_enc[i : i + 2], 16) ^ xor_key)
            for i in range(0, len(xor_enc), 2)
        )

        hex_enc = "".join(hex_enc_list)

        rot13_list.extend(
            chr(int(hex_enc[i : i + 2], 16)) for i in range(0, len(hex_enc), 2)
        )

        rot13_str = "".join(rot13_list)

        reversed_str = "".join(
            [rot13(c) if "a" <= c <= "z" or "A" <= c <= "Z" else c for c in rot13_str]
        )[::-1]

        return base64.b64decode(reversed_str.encode("utf-8")).decode("utf-8")
    except Exception:
        return


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (html_data := await network.request(url, url_num, log=log)):
        return nones

    soup = HTMLParser(html_data.content)

    iframe = soup.css_first("iframe")

    if not iframe or not (iframe_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe element found.")
        return nones

    if not (
        iframe_src_data := await network.request(
            iframe_src,
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        return nones

    dd_ptrn = re.compile(r'_dd\s?=\s?"(.*)";', re.I)
    dk_ptrn = re.compile(r"_dk\s?=\s?(\d*);", re.I)
    dri_ptrn = re.compile(r"_dri\s?=\s?(.*);", re.I)

    if not (
        (dd_mtch := dd_ptrn.search(iframe_src_data.text))
        and (dk_mtch := dk_ptrn.search(iframe_src_data.text))
        and (dri_mtch := dri_ptrn.search(iframe_src_data.text))
    ):
        log.warning(f"URL {url_num}) Failed to gather decoding variables")
        return nones

    dd, dk = dd_mtch[1], int(dk_mtch[1])
    dri: list[int] = ast.literal_eval(dri_mtch[1])

    if not (m3u_src := decrypt(dd, dk, dri)):
        log.warning(f"URL {url_num}) Decoding method failed")
        return nones

    log.info(f"URL {url_num}) Captured M3U8")

    return m3u_src, iframe_src


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    events: list[Event] = []

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    soup = HTMLParser(html_data.content)

    for card in soup.css(".card-inner"):

        if not all(
            values := [
                card.css_first(x)
                for x in (
                    ".sport-tag",
                    ".teams",
                    "a.watch-btn",
                )
            ]
        ):
            continue

        sport_elem, teams_elem, watch_btn_elem = values

        sport = sport_elem.text(strip=True).capitalize()

        sport = "Live Event" if sport == "Sports" else sport

        if not (teams := teams_elem.css(".team-name")):
            continue

        event_name = " vs ".join(team.text(strip=True) for team in teams)

        if f"[{sport}] {event_name} ({TAG})" in cached_keys:
            continue

        elif not (href := watch_btn_elem.attributes.get("href")):
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
