#!/usr/bin/env python3
import asyncio
import gzip
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from scrapers.utils import get_logger, leagues, network

log = get_logger(Path(__file__).stem)

BASE_M3U8 = Path(__file__).parent / "base.m3u8"

EPG_FILE = Path(__file__).parent / "TV.xml"

EPG_URLS = [
    f"https://epgshare01.online/epgshare01/epg_ripper_{EPG_ID}.xml.gz"
    for EPG_ID in [
        "CA2",
        "CO1",
        # "DE1",
        "DUMMY_CHANNELS",
        # "ES1",
        "FANDUEL1",
        # "FR1",
        # "IE1",
        # "PEACOCK1",
        "PLEX1",
        "UK1",
        "US2",
        "US_LOCALS1",
        # "UY1",
    ]
]

DUMMIES = {
    "Live.Event.us": leagues.live_img,
    # "WNBA.dummy.us": None,
    **{
        f"{tvg}.Dummy.us": f"https://a.espncdn.com/combiner/i?img=/i/teamlogos/leagues/500/{league}.png"
        for tvg, league in [
            ("NBA.Basketball", "nba"),
            ("NFL", "nfl"),
            ("NHL.Hockey", "nhl"),
            # ("MLB.Baseball", "mlb"),
        ]
    },
    **{
        f"{tvg}.Dummy.us": leagues.live_img
        for tvg in [
            "AUS.Rules.Football",
            "Basketball",
            "Golf",
            "PPV.EVENTS",
            "Racing",
            "Rugby",
            "Soccer",
            "Tennis",
        ]
    },
    **{
        f"{tvg}.Dummy.us": None
        for tvg in [
            "Cricket",
            "Darts",
            "Football",
        ]
    },
}

REPLACE_IDs = {
    "Ice Hockey": {
        "old": "Minor.League.Hockey.Dummy.us",
        "new": "Ice.Hockey.Dummy.us",
    },
    "NCAA Sports": {
        "old": "Sports.Dummy.us",
        "new": "NCAA.Sports.Dummy.us",
    },
    "UFC": {
        "old": "UFC.247.Dummy.us",
        "new": "UFC.Dummy.us",
    },
}


def get_tvg_ids() -> dict[str, str]:
    tvg: dict[str, str] = {}

    for line in BASE_M3U8.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#EXTINF"):
            continue

        tvg_id = re.search(r'tvg-id="([^"]*)"', line)
        tvg_logo = re.search(r'tvg-logo="([^"]*)"', line)

        if tvg_id:
            tvg[tvg_id[1]] = tvg_logo[1] if tvg_logo else None

    tvg |= DUMMIES | {v["old"]: leagues.live_img for v in REPLACE_IDs.values()}

    return tvg


async def fetch_xml(url: str) -> ET.Element | None:
    if not (xml_data := await network.request(url, log=log)):
        return

    log.info(f'Parsing XML from "{url}"')

    try:
        data = gzip.decompress(xml_data.content)

        return ET.fromstring(data)
    except Exception as e:
        log.error(f'Failed to parse XML from "{url}": {e}')
        return


def hijack_id(
    root: ET.Element,
    *,
    old: str,
    new: str,
    text: str,
) -> None:

    og_channel = root.find(f"./channel[@id='{old}']")

    if og_channel is not None:
        new_channel = ET.Element(og_channel.tag, {**og_channel.attrib, "id": new})
        icon = ET.SubElement(new_channel, "icon")
        icon.set("src", leagues.live_img)

        display_name = og_channel.find("display-name")

        if (display_name := og_channel.find("display-name")) is not None:
            new_channel.append(ET.Element("display-name", display_name.attrib))
            new_channel[-1].text = text

        for child in og_channel:
            if child.tag == "display-name":
                continue

            new_child = ET.Element(child.tag, child.attrib)
            new_child.text = child.text

        root.remove(og_channel)

        root.append(new_channel)

    for program in root.findall(f"./programme[@channel='{old}']"):
        new_program = ET.Element(program.tag, {**program.attrib, "channel": new})

        for child in program:
            new_child = ET.Element(child.tag, child.attrib)
            new_child.text = child.text
            new_program.append(new_child)

        for tag_name in ["title", "desc", "sub-title"]:
            if (tag := new_program.find(tag_name)) is not None:
                tag.text = text

        root.remove(program)

        root.append(new_program)


async def main() -> None:
    log.info(f"{'=' * 10} Fetching EPG {'=' * 10}")

    tvg_ids = get_tvg_ids()

    parsed_tvg_ids: set[str] = set()

    root = ET.Element("tv")

    epgs = await asyncio.gather(*(fetch_xml(url) for url in EPG_URLS))

    for epg_data in (epg for epg in epgs if epg is not None):
        for channel in epg_data.findall("channel"):
            if (channel_id := channel.get("id")) not in tvg_ids:
                continue

            parsed_tvg_ids.add(channel_id)

            if logo := tvg_ids.get(channel_id):
                icon_tag = channel.find("icon")

                if icon_tag is None:
                    icon_tag = ET.SubElement(channel, "icon")

                icon_tag.set("src", logo)

            if (url_tag := channel.find("url")) is not None:
                channel.remove(url_tag)

            root.append(channel)

        for program in epg_data.findall("programme"):
            if program.get("channel") not in tvg_ids:
                continue

            title_text = program.find("title").text

            subtitle = program.find("sub-title")

            if (
                title_text in ["NHL Hockey", "Live: NFL Football"]
                and subtitle is not None
            ):
                program.find("title").text = f"{title_text} {subtitle.text}"

            root.append(program)

    for title, ids in REPLACE_IDs.items():
        hijack_id(root, **ids, text=title)

    if missing_ids := tvg_ids.keys() - parsed_tvg_ids:
        log.warning(f"Missed {len(missing_ids)} TVG ID(s)")

        for channel_id in missing_ids:
            log.warning(f"Missing: {channel_id}")

    tree = ET.ElementTree(root)

    tree.write(
        EPG_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )

    log.info(f"EPG saved to {EPG_FILE.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())

    try:
        asyncio.run(network.client.aclose())
    except Exception:
        pass
