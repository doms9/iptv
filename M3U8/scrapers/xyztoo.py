from urllib.parse import parse_qsl, urlsplit

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "XYZ2"

CACHE_FILE = Cache(TAG, exp=19_800)

API_URL = "https://espn.fancy-shark151.workers.dev"


async def get_events() -> dict[str, dict[str, str | float]]:
    now = Time.rn()

    events = {}

    if not (api_req := await network.request(API_URL, log=log)):
        return events

    api_data = api_req.json()

    sport = "US Open"

    for game in api_data.get("itemListElement", []):
        title, stream_url = game.get("name"), game.get("contentUrl")

        if not (title and stream_url):
            continue

        splits = urlsplit(stream_url)

        params = dict(parse_qsl(splits.query))

        if not (stream_id := params.get("streamid")):
            continue

        key = f"[{sport}] {title} ({TAG})"

        tvg_id, logo = leagues.get_tvg_info(sport, title)

        events[key] = {
            "source": f"https://xyzstreams.blog/2/stream/espn/{stream_id}/stream_0.m3u8",
            "logo": game.get("thumbnailUrl") or logo,
            "refer": "https://xyzstreams.st/",
            "timestamp": now.timestamp(),
            "tvg-id": tvg_id or "Live.Event.us",
        }

    return events


async def scrape() -> None:
    if cached := CACHE_FILE.load():
        urls.update(cached)

        log.info(f"Loaded {len(urls)} event(s) from cache")

        return

    log.info(f'Scraping from "{API_URL}"')

    urls.update(await get_events())

    log.info(f"Collected and cached {len(urls)} new event(s)")

    CACHE_FILE.write(urls)
