import asyncio
import re
from typing import Any
from urllib.parse import urljoin

from .utils import Cache, Time, get_logger, leagues, network

log = get_logger(__name__)

urls: dict[str, dict[str, str | float]] = {}

TAG = "XYZ"

CACHE_FILE = Cache(TAG, exp=28_800)

API_FILE = Cache(f"{TAG}-api", exp=28_800)

BASE_URL = "https://xyzstreams.st/"

SPORT_URLS = {
    sport: urljoin(BASE_URL, endpoint)
    for sport, endpoint in {
        "MLB": "mlb",
        # "WNBA": "wnba",
        # "NBA",
        # "NHL",
        # "NFL": "nflembed",
    }.items()
}

API_URLS = [
    urljoin("https://site.api.espn.com/apis/site/v2/sports/", f"{sport}/scoreboard")
    for sport in [
        "baseball/mlb",
        # "basketball/nba",
        # "basketball/wnba",
        # "football/nfl",
        # "hockey/nhl",
    ]
]


async def refresh_api_cache(now: Time) -> list[dict[str, Any]]:
    tasks = [
        network.request(
            url,
            params={"dates": f"{now:%Y%m%d}"},
            headers={"User-Agent": "curl/8.20.0"},
            log=log,
        )
        for url in API_URLS
    ]

    results = await asyncio.gather(*tasks)

    api_data = []

    for resp in (r for r in results if r):
        data = resp.json()

        league = data["leagues"][0]["abbreviation"].upper()

        for event in data.get("events", []):
            event["league"] = league

            api_data.append(event)

    if not api_data:
        return [{"timestamp": now.timestamp()}]

    api_data[-1]["timestamp"] = now.timestamp()

    return api_data


async def get_sports_map() -> dict[str, dict[str, dict[str, str]]]:
    sports_map: dict[str, dict[str, dict[str, str]]] = {}

    tasks = [network.request(url, log=log) for url in SPORT_URLS.values()]

    results = await asyncio.gather(*tasks)

    if not (texts := [(html.text, html.url) for html in results if html]):
        return sports_map

    replaces = {
        "MLB": {
            "CWS": "CHW",
            # "OAK": "ATH",
            "AZ": "ARI",
            # "WAS": "WSH",
        },
        # "WNBA": {
        #     "GSV": "GS",
        #     "LVA": "LV",
        #     "LAS": "LA",
        #     "NYL": "NY",
        #     "PHO": "PHX",
        #     "PDX": "POR",
        #     "WAS": "WSH",
        # },
        # "NFL": {
        #     "WAS": "WSH",
        # },
    }

    ptrn = re.compile(r"M3U8_CHANNELS_MAP\s*=\s*\{(.*?)\};", re.S)

    for text, url in texts:
        sport = next((k for k, v in SPORT_URLS.items() if v == url), "Live Event")

        if not (match := ptrn.search(text)):
            sports_map[sport] = {}

        else:
            pairs: list[tuple[str, str]] = re.findall(
                r"'([^']+)'\s*:\s*'([^']+)'",
                match[1],
            )

            sports_map[sport] = dict(pairs)

    for sport, abbrs in replaces.items():
        for old, new in abbrs.items():
            sports_map[sport][new] = sports_map[sport].pop(old, {})

    return sports_map


async def get_events() -> dict[str, dict[str, str | float]]:
    now = Time.rn()

    events: dict[str, dict[str, str | float]] = {}

    if not (api_data := API_FILE.load(per_entry=False, ts_index=-1)):
        log.info("Refreshing API cache")

        api_data = await refresh_api_cache(now)

        API_FILE.write(api_data)

    if not (sports_map := await get_sports_map()):
        return events

    for game_info in api_data:
        if not all(
            values := [
                game_info.get(x)
                for x in (
                    "league",
                    "name",
                    "shortName",
                )
            ]
        ):
            continue

        sport, name, short_name = values

        for abbr in re.sub(r"(@|VS)", "", short_name, flags=re.I).split():
            key = f"[{sport}] {name} | {abbr} Feed ({TAG})"

            tvg_id, logo = leagues.get_tvg_info(sport, name)

            events[key] = {
                "source": sports_map.get(sport, {}).get(abbr),
                "logo": logo,
                "refer": BASE_URL,
                "timestamp": now.timestamp(),
                "tvg-id": tvg_id or "Live.Event.us",
            }

    return events


async def scrape() -> None:
    if cached_urls := CACHE_FILE.load():
        urls.update({k: v for k, v in cached_urls.items() if v["source"]})

        log.info(f"Loaded {len(urls)} event(s) from cache")

        return

    log.info(f'Scraping from "{BASE_URL}"')

    urls.update(await get_events())

    (
        log.info(f"Collected and cached {new_urls} event(s)")
        if (new_urls := len(urls))
        else log.info("No events found")
    )

    CACHE_FILE.write(urls)
