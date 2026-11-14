import asyncio
import re
from functools import partial
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import Browser, Page, Request

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "GSPORTS"

CACHE_FILE = Cache(TAG, exp=19_800)

BASE_URL = "https://blogs.st/meth/"


def capture_req(
    req: Request,
    captured: list[str],
    got_one: asyncio.Event,
) -> None:

    pattern = re.compile(r"^https?://mixedshop.pw")

    if pattern.search(req.url):
        captured.append(req.url)
        got_one.set()


async def process_event(
    url: str,
    url_num: int,
    page: Page,
) -> str | None:

    captured: list[str] = []

    got_one = asyncio.Event()

    handler = partial(
        capture_req,
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

        if not resp or not resp.ok:
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


async def get_events() -> list[Event]:
    now = Time.rn()

    events: list[Event] = []

    if not (api_req := await network.request(urljoin(BASE_URL, "?api"), log=log)):
        return events

    api_data: dict[str, Any] = api_req.json()

    if not (updated_date := api_data.get("updated")):
        return events

    updated_dt = Time.from_str(updated_date, tz_name="UTC")

    if now.date() != updated_dt.date():
        return events

    for game in api_data.get("events", []):
        if not all(
            values := [
                game.get(x)
                for x in (
                    "title",
                    "category",
                    "url",
                )
            ]
        ):
            continue

        event_name, sport, url = values

        events.append(
            Event(
                sport=sport,
                name=event_name,
                link=url,
                timestamp=now.timestamp(),
            )
        )

    return events


async def scrape(browser: Browser) -> None:
    if cached_sources := CACHE_FILE.load():
        urls.update({k: v for k, v in cached_sources.items() if v["source"]})

        log.info(f"Loaded {len(urls)} event(s) from cache")

        return

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events():
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

                    key = f"[{ev.sport}] {ev.name} ({TAG})"

                    tvg_id, logo = leagues.get_tvg_info(ev.sport, ev.name)

                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": ev.link,
                        "origin": "https://blogs.st",
                        "timestamp": ev.timestamp,
                        "tvg-id": tvg_id or "Live.Event.us",
                    }

                    cached_sources[key] = entry

                    if source:
                        urls[key] = entry

        log.info(f"Collected and cached {len(urls)} event(s)")

    else:
        log.info("No new events found")

    CACHE_FILE.write(cached_sources)
