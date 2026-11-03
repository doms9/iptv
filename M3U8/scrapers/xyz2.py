import base64
import hashlib
import random
import re
from collections.abc import KeysView
from functools import partial
from urllib.parse import urljoin, urlsplit

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "XYZ2"

CACHE_FILE = Cache(TAG, exp=10_800)

HTML_FILE = Cache(f"{TAG}-html", exp=19_800)

BASE_URL = "https://xyzstreams.st"

SERVERS = [
    # "https://xyzstreams.blog/1/",
    # "https://tokenized.b-cdn.net/",
    # "https://xyzstreams.space/",
    # "https://eu-hlss2.b-cdn.net/",
    # "https://hlss2.b-cdn.net/",
    # "https://us2-hlss2.b-cdn.net/",
    # "https://hls.fancy-shark151.workers.dev/",
    "https://eu-4605069466.duckdns.org",
    # "https://ierewaw2.b-cdn.net",
    "https://hls.kozow.com",
    # "https://xyzstream.duckdns.org",
    "https://us-xyz.work.gd",
]

KEY = "TXlTdXBlclNlY3JldEtleTEyMyE="


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

    if not (
        html_data := await network.request(
            url,
            url_num,
            headers={"Referer": BASE_URL},
            log=log,
        )
    ):
        return

    elif not (
        api_data := await network.client_unv.get(
            urljoin(server, "api/token"),
            headers={"Referer": url},
        )
    ):
        return

    token_data = api_data.json()

    token, token_iv = token_data.get("token"), token_data.get("iv")

    if not (token and token_iv):
        log.warning(f"URL {url_num}) Unable to get token information")
        return

    if "stream.html" in url:
        ifr_src = url.replace("stream.html", "embed")

    else:
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

    if not (html_data := await network.request(urljoin(BASE_URL, "cbb"), log=log)):
        return events

    soup = HTMLParser(html_data.content)

    for game in soup.css("ul.schedule-list > a"):
        if not (href := game.attributes.get("href")):
            continue

        elif not (teams := [i.text(strip=True) for i in game.css(".team-group") if i]):
            continue

        elif not (meta_elem := game.css_first(".game-meta")):
            continue

        meta = meta_elem.text(strip=True)

        event_name = (
            teams[0] if len(teams) == 1 else " vs ".join(i.strip() for i in teams)
        )

        event_name = re.sub(r"(#\d{1,2})", r"\g<1> ", event_name)

        event_time = meta.split("•", 1)[0].strip()

        event_dt = Time.from_str(
            f"{now.date()} {event_time}",
            fmt="%Y-%m-%d %I:%M %p ET",
            tz_name="EST",
        )

        key = f"[College Basketball] {event_name} ({TAG})"

        events[key] = {
            "sport": "College Basketball",
            "name": event_name,
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

    start_ts = now.delta(minutes=-90).timestamp()
    end_ts = now.delta(minutes=30).timestamp()

    return [
        Event(**v)
        for k, v in events.items()
        if k not in cached_keys and (start_ts <= v["event_ts"] <= end_ts)
    ]


async def scrape() -> None:
    cached_sources = CACHE_FILE.load()

    valid_sources = {k: v for k, v in cached_sources.items() if v["source"]}

    valid_count = cached_count = len(valid_sources)

    urls.update(valid_sources)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_sources.keys()):
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
                "logo": logo,
                "refer": ev.link,
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
            }

            cached_sources[key] = entry

            if source:
                valid_count += 1

                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_sources)
