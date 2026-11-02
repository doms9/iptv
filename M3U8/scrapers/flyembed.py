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

BASE_URL = "https://flyembed.cfd/"

API_URL = "https://ovogoal.pages.dev/api/v2/flyembed2.json"


def cleanup(s: str) -> str:
    return re.sub(r"(\r|\n|\t)", "", s).strip()


def decode_m3u(m_url: str, resp_b: bytes) -> str:
    tk = m_url.rsplit("/", 1)[-1].encode()

    s = resp_b[0]

    return bytes(
        c ^ tk[i % len(tk)] ^ ((s + i) & 255) for i, c in enumerate(resp_b[1:])
    ).decode("latin-1")


async def process_event(url: str, url_num: int) -> tuple[str | None, str | None]:
    nones = None, None

    if not (
        html_data := await network.request(
            url,
            url_num,
            headers={"Referer": BASE_URL},
            log=log,
        )
    ):
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

    num_list_ptrn = re.compile(
        r"var\s+_\w+\s*=\s*\[([\d,\s]+)\]\s*,\s*_\w+\s*=\s*(\d+)\s*,\s*_\w+\s*=\s*(\d+)",
        re.S,
    )

    m3u_ptrn = re.compile(r'loadStreamUrl\("([^"]*)",', re.I)

    if not (num_list_mtch := num_list_ptrn.search(iframe_src_data.text)):
        log.warning(f"URL {url_num}) Unable to decipher m3u encryption.")
        return nones

    nums, x, y = num_list_mtch[1], int(num_list_mtch[2]), int(num_list_mtch[3])

    js = "".join(
        chr(((int(n) ^ x) - y + 256) % 256) for n in nums.split(",") if n.strip()
    )

    if not (m3u_mtch := m3u_ptrn.search(js)):
        log.warning(f"URL {url_num}) No M3U8 source found.")
        return nones

    elif not (
        m3u_resp := await network.request(
            m3u_mtch[1],
            url_num,
            headers={"Referer": iframe_src},
            log=log,
        )
    ):
        return

    log.info(f"URL {url_num}) Captured M3U8")

    return decode_m3u(f"{m3u_resp.url}", m3u_resp.content), iframe_src


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    now = Time.rn()

    events: list[Event] = []

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")

        api_data = [{"timestamp": now.timestamp()}]

        if r := await network.request(API_URL, log=log):
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
                    "Team1",
                    "Team2",
                    "MatchDate",
                    "MatchStartTime",
                    "IframeURL",
                )
            ]
        ):
            continue

        sport, away, home, date, time, link = values

        event_dt = Time.from_str(f"{date} {time}", tz_name="ALMT")

        if not start_dt <= event_dt <= end_dt:
            continue

        name = f"{away.strip()} vs {home.strip()}"

        sport, name = cleanup(sport), cleanup(name)

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
    cached_sources = CACHE_FILE.load()

    valid_sources = {k: v for k, v in cached_sources.items() if v["source"]}

    valid_count = cached_count = len(valid_sources)

    urls.update(valid_sources)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_sources.keys()):
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

            cached_sources[key] = entry

            if source:
                valid_count += 1

                urls[key] = entry

        log.info(f"Collected and cached {valid_count - cached_count} new event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_sources)
