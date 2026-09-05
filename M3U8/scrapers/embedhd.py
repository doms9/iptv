import asyncio
from collections.abc import KeysView
from functools import partial
from urllib.parse import urljoin

from playwright.async_api import Browser, Page

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "EMBEDHD"

CACHE_FILE = Cache(TAG, exp=5_400)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL = "https://embedhd.st"


def fix_league(s: str) -> str:
    splits = s.split()

    i = splits[0]

    return f"{i.upper() if len(i) <= 5 else i.capitalize()} {' '.join(x.capitalize() for x in splits[1:])}".strip()


async def process_event(
    url: str,
    url_num: int,
    page: Page,
) -> str | None:

    captured: list[str] = []

    got_one = asyncio.Event()

    handler = partial(
        network.capture_req,
        captured=captured,
        got_one=got_one,
    )

    page.on("request", handler)

    try:
        resp = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=6_000,
            referer=BASE_URL,
        )

        if not resp or resp.status != 200:
            log.error(f"URL {url_num}) Status Code: {resp.status if resp else 'None'}")
            return

        wait_task = asyncio.create_task(got_one.wait())

        try:
            await asyncio.wait_for(wait_task, timeout=6)
        except TimeoutError:
            log.warning(f"URL {url_num}) Timed out waiting for M3U8.")
            return

        finally:
            if not wait_task.done():
                wait_task.cancel()

                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass

        if captured:
            log.info(f"URL {url_num}) Captured M3U8")
            return captured[0]

    except Exception as e:
        log.warning(f"URL {url_num}) {e}")
        return

    finally:
        page.remove_listener("request", handler)


async def get_events(cached_keys: KeysView[str]) -> list[Event]:
    now = Time.rn()

    if not (api_data := API_FILE.load(per_entry=False)):
        log.info("Refreshing API cache")

        api_data = {"timestamp": now.timestamp()}

        if r := await network.request(urljoin(BASE_URL, "api-event.php"), log=log):
            api_data: dict = r.json()

            api_data["timestamp"] = now.timestamp()

        API_FILE.write(api_data)

    events: list[Event] = []

    start_dt = now.delta(hours=-3)
    end_dt = now.delta(minutes=30)

    for info in api_data.get("days", []):
        for event in info["items"]:
            if (event_league := event["league"]) == "channel tv":
                continue

            event_dt = Time.from_ts(event["ts_et"])

            if not start_dt <= event_dt <= end_dt:
                continue

            sport = fix_league(event_league)

            event_name = event["title"]

            if f"[{sport}] {event_name} ({TAG})" in cached_keys:
                continue

            if not (event_streams := event["streams"]):
                continue

            elif not (event_link := event_streams[0].get("link")):
                continue

            events.append(
                Event(
                    sport=sport,
                    name=event_name,
                    link=event_link,
                    timestamp=now.timestamp(),
                )
            )

    return events


async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load()

    valid_urls = {k: v for k, v in cached_urls.items() if v["source"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_urls.keys()):
        log.info(f"Processing {len(events)} new URL(s)")

        async with network.event_context(browser) as context:
            for i, ev in enumerate(events, start=1):
                async with network.event_page(context) as page:
                    handler = partial(
                        process_event,
                        url=ev.link,
                        url_num=i,
                        page=page,
                    )

                    source = await network.safe_process(
                        handler,
                        url_num=i,
                        semaphore=network.PW_S,
                        log=log,
                    )

                    tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

                    key = f"[{ev.sport}] {ev.name} ({TAG})"

                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": "https://forgemindly.com",
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
