import asyncio
import re
from functools import partial
from urllib.parse import urljoin

from playwright.async_api import Browser, Page, TimeoutError
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .utils import Cache, Event, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "SPRTSPASS"

CACHE_FILE = Cache(TAG, exp=10_800)

BASE_URL = "https://streamseast.ws"

SPORT_URLS = {
    sport: urljoin(BASE_URL, sport.lower())
    for sport in [
        # "Boxing",
        # "F1",
        "MLB",
        # "MMA",
        # "NBA",
        "NFL",
        # "NHL",
        # "Soccer",
    ]
}


async def process_event(
    url: str,
    url_num: int,
    page: Page,
) -> tuple[str | None, str | None, str | None]:

    nones = None, None

    captured: list[str] = []

    got_one = asyncio.Event()

    handler = partial(
        network.capture_req,
        captured=captured,
        got_one=got_one,
    )

    page.on("request", handler)

    event_name = "Sporting Event"

    try:
        resp = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=6_000,
        )

        if not resp or resp.status != 200:
            log.error(f"URL {url_num}) Status Code: {resp.status if resp else 'None'}")
            return (event_name, *nones)

        event_name_elem = page.locator("h1.match-head")

        event_name = await event_name_elem.inner_text(timeout=1_250)

        try:
            ifr = page.locator("iframe.embed-responsive-item")

            await ifr.wait_for(timeout=1_250)

            ifr_src = await ifr.get_attribute("src")
        except TimeoutError:
            log.warning(f"URL {url_num}) No iframe found.")
            return (event_name, *nones)

        await page.goto(
            ifr_src,
            wait_until="domcontentloaded",
            timeout=2_250,
        )

        wait_task = asyncio.create_task(got_one.wait())

        try:
            await asyncio.wait_for(wait_task, timeout=5)
        except TimeoutError:
            log.warning(f"URL {url_num}) Timed out waiting for M3U8.")
            return (event_name, *nones)

        finally:
            if not wait_task.done():
                wait_task.cancel()

                try:
                    await wait_task
                except asyncio.CancelledError:
                    pass

        if captured:
            if "indianservers" in captured[0].lower():
                log.warning(f"URL {url_num}) Unsuitable M3U8 link captured.")
                return (event_name, *nones)

            log.info(f"URL {url_num}) Captured M3U8")

            return event_name, ifr_src, captured[0]
    except Exception as e:
        log.warning(f"URL {url_num}) {e}")
        return (event_name, *nones)

    finally:
        page.remove_listener("request", handler)


async def get_events(cached_links: set[str]) -> list[Event]:
    now = Time.rn()

    tasks = [network.request(url, log=log) for url in SPORT_URLS.values()]

    results = await asyncio.gather(*tasks)

    events: list[Event] = []

    if not (
        soups := [(HTMLParser(html.content), html.url) for html in results if html]
    ):
        return events

    start_dt = now.delta(hours=-3)
    end_dt = now.delta(minutes=5)

    date_ptrn = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", re.I)

    for soup, url in soups:
        sport = next((k for k, v in SPORT_URLS.items() if v == url), "Live Event")

        for event in soup.css("a.matches"):
            if not (href := event.attributes.get("href")):
                continue

            elif (link := urljoin(BASE_URL, href)) in cached_links:
                continue

            if (scr_elem := event.css_first("script")) and (
                match := date_ptrn.search(scr_elem.text(strip=True))
            ):
                event_dt = Time.fromisoformat(match[0]).to_tz("EST")

            elif event.css_first("span.status-badge.badge.bg-success"):
                event_dt = now

            else:
                continue

            if not start_dt <= event_dt <= end_dt:
                continue

            events.append(
                Event(
                    sport=sport,
                    link=link,
                    timestamp=event_dt.timestamp(),
                )
            )

    return events


async def scrape(browser: Browser) -> None:
    cached_urls = CACHE_FILE.load()

    cached_links = {entry["link"] for entry in cached_urls.values()}

    valid_urls = {k: v for k, v in cached_urls.items() if v["source"]}

    valid_count = cached_count = len(valid_urls)

    urls.update(valid_urls)

    log.info(f"Loaded {cached_count} event(s) from cache")

    log.info(f'Scraping from "{BASE_URL}"')

    if events := await get_events(cached_links):
        log.info(f"Processing {len(events)} URL(s)")

        async with network.event_context(browser, stealth=False) as context:
            for i, ev in enumerate(events, start=1):
                async with network.event_page(context) as page:
                    handler = partial(
                        process_event,
                        url=ev.link,
                        url_num=i,
                        page=page,
                    )

                    name, ifr_src, source = await network.safe_process(
                        handler,
                        url_num=i,
                        timeout_return=(None, None, None),
                        semaphore=network.PW_S,
                        log=log,
                    )

                    tvg_id, logo = leagues.get_tvg_info(ev.sport, name)

                    key = f"[{ev.sport}] {name} ({TAG})"

                    entry = {
                        "source": source,
                        "logo": logo,
                        "refer": ifr_src,
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
