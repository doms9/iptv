import asyncio
import logging
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import cache, partial
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit, urlunsplit

import adblock
import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    Route,
)

from .config import Time
from .logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

_TYPE_MAP = {"xhr": "xmlhttprequest", "fetch": "xmlhttprequest"}


class Network:
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
    )

    HTTP_S = asyncio.Semaphore(10)

    PW_S = asyncio.Semaphore(3)

    def __init__(self) -> None:
        client_params = {
            "timeout": httpx.Timeout(5.0),
            "follow_redirects": True,
            "headers": {"User-Agent": Network.UA},
            "http2": True,
        }

        self.client = httpx.AsyncClient(**client_params)

    async def setup_adblock(self) -> None:
        easylist_file = Path(__file__).parent / "easylist.txt"

        easylist = easylist_file.read_text(encoding="utf-8")

        if Time.rn().timestamp() - easylist_file.stat().st_mtime > 172_500:
            logger.info("Updating EasyList file")

            if r := await self.request(
                "https://easylist-downloads.adblockplus.org/easylist.txt"
            ):
                easylist_file.write_bytes(r.content)
                easylist = r.text

            else:
                logger.warning("Unable to update EasyList file")

        filter_set = adblock.FilterSet()
        filter_set.add_filter_list(easylist)
        self.engine = adblock.Engine(filter_set)

    async def request(
        self,
        url: str,
        url_num: int | None = None,
        log: logging.Logger | None = None,
        **kwargs,
    ) -> httpx.Response | None:

        log = log or logger

        try:
            r = await self.client.get(url, **kwargs)

            r.raise_for_status()

            return r
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            log.error(
                f'URL {url_num}) Failed to fetch "{url}": {e}'
                if url_num
                else f'Failed to fetch "{url}": {e}'
            )

            return

    @staticmethod
    def ensure_https(url: str) -> str:
        splits = urlsplit(url)

        if not splits.scheme:
            splits = splits._replace(scheme="https")

        return urlunsplit(splits)

    @staticmethod
    async def safe_process(
        fn: Callable[[], Awaitable[T]],
        url_num: int,
        semaphore: asyncio.Semaphore,
        timeout: int | float = 30,
        timeout_return: T | None = None,
        log: logging.Logger | None = None,
    ) -> T | None:

        log = log or logger

        async with semaphore:
            task = asyncio.create_task(fn())

            try:
                return await asyncio.wait_for(task, timeout=timeout)

            except TimeoutError:
                log.warning(
                    f"URL {url_num}) Timed out after {timeout}s, skipping event"
                )

                task.cancel()

                try:
                    await task
                except asyncio.CancelledError:
                    pass

                except Exception as e:
                    log.warning(f"URL {url_num}) Ignore exception after timeout: {e}")

                return timeout_return
            except Exception as e:
                log.error(f"URL {url_num}) Unexpected error: {e}")

                return timeout_return

    @cache
    @staticmethod
    def stealth_js() -> str:
        return (Path(__file__).parent / "stealth.js").read_text(encoding="utf-8")

    def to_block(self, request: Request) -> bool:
        req_type = _TYPE_MAP.get(request.resource_type, request.resource_type)

        result = self.engine.check_network_urls(
            url=request.url,
            source_url=request.frame.url,
            request_type=req_type,
        )

        return result.matched

    async def _adblock(self, route: Route) -> None:
        request = route.request

        if request.resource_type not in ("script", "image", "media", "xhr", "fetch"):
            await route.continue_()

            return

        await route.abort() if self.to_block(request) else await route.continue_()

    @asynccontextmanager
    async def event_context(
        self,
        browser: Browser,
        stealth: bool = True,
        ignore_https: bool = False,
    ) -> AsyncGenerator[BrowserContext]:

        context: BrowserContext | None = None

        try:
            if stealth:
                context = await browser.new_context(
                    user_agent=self.UA,
                    ignore_https_errors=ignore_https,
                    viewport={"width": 1366, "height": 768},
                    device_scale_factor=1,
                    locale="en-US",
                    timezone_id="America/New_York",
                    color_scheme="dark",
                    extra_http_headers=(
                        {
                            "Accept-Language": "en-US,en;q=0.9",
                            "Upgrade-Insecure-Requests": "1",
                        }
                    ),
                )

                await context.add_init_script(script=Network.stealth_js())

                await context.route("**/*", self._adblock)

            else:
                context = await browser.new_context()

            yield context

        finally:
            if context:
                await context.close()

    @staticmethod
    @asynccontextmanager
    async def event_page(context: BrowserContext) -> AsyncGenerator[Page]:
        page = await context.new_page()

        try:
            yield page

        finally:
            await page.close()

    @staticmethod
    async def browser(playwright: Playwright, external: bool = False) -> Browser:
        return (
            await playwright.chromium.connect_over_cdp("http://localhost:9222")
            if external
            else await playwright.firefox.launch(headless=True)
        )

    @staticmethod
    def capture_req(
        req: Request,
        captured: list[str],
        got_one: asyncio.Event,
    ) -> None:

        blocked = [
            re.escape(i)
            for i in {
                # "amazonaws",
                "knitcdn",
                "jwpltx",
            }
        ]

        pattern = re.compile(rf"^(?!.*({'|'.join(blocked)})).*\.m3u8", re.I)

        if pattern.search(req.url):
            captured.append(req.url)
            got_one.set()

    async def process_event(
        self,
        url: str,
        url_num: int,
        page: Page,
        timeout: int | float = 10,
        log: logging.Logger | None = None,
    ) -> str | None:

        log = log or logger

        captured: list[str] = []

        got_one = asyncio.Event()

        handler = partial(
            self.capture_req,
            captured=captured,
            got_one=got_one,
        )

        page.on("request", handler)

        try:
            resp = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=6_000,
            )

            if not resp or resp.status != 200:
                log.error(
                    f"URL {url_num}) Status Code: {resp.status if resp else 'None'}"
                )
                return

            wait_task = asyncio.create_task(got_one.wait())

            try:
                await asyncio.wait_for(wait_task, timeout=timeout)
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


network = Network()

__all__ = ["network"]
