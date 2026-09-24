import base64
import hashlib
import json
import random
import re
from collections.abc import KeysView
from dataclasses import dataclass
from functools import partial
from urllib.parse import urljoin, urlsplit

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "XYZ"

CACHE_FILE = Cache(TAG, exp=10_800)

HTML_FILE = Cache(f"{TAG}-html", exp=28_800)

BASE_URL = "https://xyzstreams.st"

SERVERS = [
    "https://xyzstreams.blog/1/",
    # "https://tokenized.b-cdn.net/",
    "https://xyzstreams.space/",
]

KEY = "TXlTdXBlclNlY3JldEtleTEyMyE="


@dataclass(kw_only=True, slots=True)
class XYZEvent(Event):
    logo: str | None = None


def decrypt_token(
    secret: str,
    token_h: str,
    iv_h: str,
) -> str:

    key = hashlib.sha256(secret.encode("utf-8")).digest()

    iv = bytes.fromhex(iv_h)

    cipher_t = bytes.fromhex(token_h)

    cipher = AES.new(key, AES.MODE_CBC, iv)

    dec = unpad(cipher.decrypt(cipher_t), AES.block_size)

    raw = dec.decode("utf-8", errors="ignore")

    return re.sub(r"[^\x20-\x7E]", "", raw).strip()


async def process_event(url: str, url_num: int) -> str | None:
    server = random.choice(SERVERS)

    if not (html_data := await network.request(url, url_num, log=log)):
        return

    elif not (
        api_data := await network.request(
            urljoin(server, "api/token"),
            url_num,
            headers={"Referer": url},
            log=log,
        )
    ):
        return

    token_data = api_data.json()

    token, token_iv = token_data.get("token"), token_data.get("iv")

    if not (token and token_iv):
        log.warning(f"URL {url_num}) Failed to get token information")
        return

    soup = HTMLParser(html_data.content)

    iframe = soup.css_first("iframe#stream-player")

    if not iframe or not (ifr_src := iframe.attributes.get("src")):
        log.warning(f"URL {url_num}) No iframe source found.")
        return

    strm_path = urlsplit(ifr_src).query

    raw_token = decrypt_token(
        base64.b64decode(KEY).decode("utf-8"),
        token,
        token_iv,
    )

    log.info(f"URL {url_num}) Captured M3U8")

    return urljoin(server, f"{strm_path}/mono.ts.m3u8?token={raw_token}")


async def refresh_html_cache(now: Time) -> dict[str, dict[str, str | float]]:
    events = {}

    if not (html_data := await network.request(BASE_URL, log=log)):
        return events

    ptrn = re.compile(
        r"(?:const|let|var)\s+EVENTS_DATA\s*=\s*(\[.*?\])\s*;",
        re.S | re.I,
    )

    if not (match := ptrn.search(html_data.text)):
        log.warning('Failed to find "EVENTS_DATA" var.')
        return events

    event_data: list[dict[str, str]] = json.loads(match[1])

    for game in event_data:
        if not all(
            values := [
                game.get(x)
                for x in (
                    "category",
                    "title",
                    "start",
                    "href",
                )
            ]
        ):
            continue

        sport, name, event_time, href = values

        event_dt = Time.fromisoformat(event_time).to_tz("EST")

        if event_dt.date() != now.date():
            continue

        key = f"[{sport}] {name} ({TAG})"

        events[key] = {
            "sport": sport,
            "name": name,
            "logo": game.get("bg"),
            "link": urljoin(BASE_URL, href),
            "event_ts": event_dt.timestamp(),
            "timestamp": now.timestamp(),
        }

    return events


async def get_events(cached_keys: KeysView[str]) -> list[XYZEvent]:
    now = Time.rn()

    if not (events := HTML_FILE.load()):
        log.info("Refreshing HTML cache")

        events = await refresh_html_cache(now)

        HTML_FILE.write(events)

    start_ts = now.delta(minutes=-30).timestamp()
    end_ts = now.delta(minutes=30).timestamp()

    return [
        XYZEvent(**v)
        for k, v in events.items()
        if k not in cached_keys and (start_ts <= v["event_ts"] <= end_ts)
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

        now = Time.rn()

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
                "logo": ev.logo or logo,
                "refer": ev.link,
                "timestamp": now.timestamp(),
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
