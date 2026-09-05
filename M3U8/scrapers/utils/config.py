import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(kw_only=True, slots=True)
class Event:
    sport: str
    name: str | None = None
    link: str
    event_ts: float | None = None
    timestamp: float | None = None


class Time(datetime):
    __slots__ = ()

    ZONES: dict[str, ZoneInfo] = {
        # "CET": ZoneInfo("Europe/Berlin"),
        "ET": ZoneInfo("America/New_York"),
        "GMT": ZoneInfo("Europe/London"),
        # "MSK": ZoneInfo("Europe/Moscow"),
        # "PST": ZoneInfo("America/Los_Angeles"),
        "UTC": ZoneInfo("UTC"),
    }

    ZONES["EST"] = ZONES["ET"]

    TZ = ZONES["ET"]

    @classmethod
    def rn(cls) -> "Time":
        return cls.now(tz=cls.TZ).replace(second=0, microsecond=0)

    @classmethod
    def from_ts(cls, ts: int | float) -> "Time":
        return cls.fromtimestamp(ts, tz=cls.TZ)

    @classmethod
    def default_8(cls) -> float:
        return cls.now().replace(hour=8, minute=0, second=0, microsecond=0).timestamp()

    def delta(self, **kwargs) -> "Time":
        return self + timedelta(**kwargs)

    def to_tz(self, tzone: str) -> "Time":
        return self.__class__.fromtimestamp(self.timestamp(), tz=self.ZONES[tzone])

    @classmethod
    def __to_class_tz(cls, dt: datetime) -> "Time":
        return cls.fromtimestamp(dt.timestamp(), tz=cls.TZ)

    @classmethod
    def from_str(
        cls,
        s: str,
        fmt: str | None = None,
        tz_name: str | None = None,
    ) -> "Time":

        tz = cls.ZONES[tz_name] if tz_name else cls.TZ

        if fmt:
            dt = datetime.strptime(s, fmt).replace(tzinfo=tz)

        else:
            formats = [
                "%b %d, %Y %H:%M %Z",
                "%B %d, %Y %H:%M",
                "%d %B,%Y %I:%M %p",
                "%d %B,%Y %H:%M %p",
                "%d %B ,%Y %I:%M %p",
                "%d %B ,%Y %H:%M %p",
                "%d%B,%Y %H:%M %p",
                "%B %d, %Y %I:%M %p",
                "%B %d, %Y %I:%M:%S %p",
                "%B %d, %Y %H:%M:%S",
                "%Y-%m-%d",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %I:%M %p",
                "%Y-%m-%d %H:%M %p",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M",
                "%Y/%m/%d %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%m/%d/%Y %H:%M",
                "%m/%d/%Y %I:%M %p",
                "%m/%d/%Y %H:%M:%S",
                "%d/%m/%Y %I:%M %p",
                "%a, %d %b %Y %H:%M",
                "%a, %d %b %Y %H:%M:%S %z",
                "%A, %b %d, %Y %H:%M",
            ]

            for frmt in formats:
                try:
                    dt = datetime.strptime(s, frmt)
                    break
                except ValueError:
                    continue
            else:
                return cls.from_ts(cls.default_8())

            if not dt.tzinfo:
                dt = dt.replace(tzinfo=tz)

        return cls.__to_class_tz(dt)


class Leagues:
    live_img = "https://i.gyazo.com/4a5e9fa2525808ee4b65002b56d3450e.png"

    def __init__(self) -> None:
        self.data = json.loads(
            (Path(__file__).parent / "sports.json").read_text(encoding="utf-8")
        )

    def teams(self, league: str) -> list[str]:
        return self.data["teams"].get(league, [])

    def info(self, sport: str) -> tuple[str | None, str]:
        sport = sport.upper()

        if match := next(
            (
                (tvg_id, league_data.get("logo"))
                for tvg_id, leagues in self.data["leagues"].items()
                for league_entry in leagues
                for league_name, league_data in league_entry.items()
                if sport == league_name or sport in league_data.get("aliases", [])
            ),
            None,
        ):
            tvg_id, logo = match

            return (tvg_id, logo or self.live_img)

        return (None, self.live_img)

    def is_valid(
        self,
        event: str,
        league: str,
    ) -> bool:

        pattern = re.compile(r"\s+(?:-|vs\.?|at|@)\s+", re.I)

        if pattern.search(event):
            t1, t2 = pattern.split(event)[:2]

            return any(t in self.teams(league) for t in (t1.strip(), t2.strip()))

        return False

    def get_tvg_info(
        self,
        sport: str,
        name: str,
    ) -> tuple[str | None, str]:

        match sport:
            case "American Football" | "NFL":
                if self.is_valid(name, "NFL"):
                    return self.info("NFL")

                elif self.is_valid(name, "CFL"):
                    return self.info("CFL")

                return self.info("American Football")

            case "Basketball" | "NBA":
                if self.is_valid(name, "NBA"):
                    return self.info("NBA")

                elif self.is_valid(name, "WNBA"):
                    return self.info("WNBA")

                return self.info("Basketball")

            case "Ice Hockey" | "Hockey":
                return (
                    self.info("NHL")
                    if self.is_valid(name, "NHL")
                    else self.info("Hockey")
                )

            case "Baseball" | "MLB":
                return (
                    self.info("MLB")
                    if self.is_valid(name, "MLB")
                    else self.info("Baseball")
                )

            case _:
                return self.info(sport)


leagues = Leagues()

__all__ = ["leagues", "Event", "Time"]
