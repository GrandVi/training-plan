"""Resolve the athlete's thresholds from the profile: estimates and sanity warnings."""

import datetime as dt
import math

from pydantic import BaseModel, ConfigDict

from endurance_planner.models.enums import Severity, Sport
from endurance_planner.models.issues import Issue
from endurance_planner.models.profile import Profile
from endurance_planner.units import format_clock

FTP_FROM_20MIN = 0.95  # FTP ~ 95% of best 20-min power (Allen & Coggan)
RIEGEL_EXPONENT = 1.06  # T2 = T1 * (D2 / D1) ** 1.06 (Riegel 1981)
THRESHOLD_RACE_S = 3600  # threshold pace ~ pace sustainable for a 60-min race
STALE_TEST_DAYS = 8 * 7
PLAUSIBLE_W_PER_KG = (1.0, 6.5)
PLAUSIBLE_THRESHOLD_PACE_S = (170, 540)  # 2:50 to 9:00 /km
PLAUSIBLE_LTHR_OVER_MAX = (0.75, 0.97)
RIEGEL_RELIABLE_RACE_S = (15 * 60, 150 * 60)


class ResolvedThresholds(BaseModel):
    """Thresholds actually used for targets; `estimated` lists the derived fields."""

    model_config = ConfigDict(frozen=True)

    ftp_w: float | None = None
    bike_lthr_bpm: int | None = None
    bike_max_hr_bpm: int | None = None
    threshold_pace_s: float | None = None  # s/km
    run_lthr_bpm: int | None = None
    run_max_hr_bpm: int | None = None
    estimated: tuple[str, ...] = ()

    def lthr(self, sport: Sport) -> int | None:
        return self.bike_lthr_bpm if sport is Sport.BIKE else self.run_lthr_bpm


def ftp_from_20min(power_w: float) -> float:
    return FTP_FROM_20MIN * power_w


def threshold_pace_from_race(distance_km: float, time_s: float) -> float:
    """Threshold pace (s/km) as the Riegel-predicted pace of a 60-min race."""
    hour_distance_km = distance_km * math.pow(THRESHOLD_RACE_S / time_s, 1 / RIEGEL_EXPONENT)
    return THRESHOLD_RACE_S / hour_distance_km


def max_hr_from_age(age: int) -> int:
    """Tanaka et al. 2001: 208 - 0.7 * age."""
    return round(208 - 0.7 * age)


def resolve_thresholds(profile: Profile, today: dt.date) -> tuple[ResolvedThresholds, list[Issue]]:
    issues: list[Issue] = []
    estimated: list[str] = []

    def warn(code: str, message: str) -> None:
        issues.append(Issue(severity=Severity.WARNING, code=code, message=message))

    def check_test_date(label: str, date: dt.date | None) -> None:
        if date is None:
            return
        age_days = (today - date).days
        if age_days > STALE_TEST_DAYS:
            warn("stale_test", f"{label} test is {age_days // 7} weeks old (> 8); consider retest")
        elif age_days < 0:
            warn("future_test_date", f"{label} test date {date} is in the future")

    def check_hr(label: str, lthr: int | None, max_hr: int | None) -> None:
        if lthr is None or max_hr is None:
            return
        low, high = PLAUSIBLE_LTHR_OVER_MAX
        if lthr >= max_hr:
            warn("lthr_above_max_hr", f"{label} LTHR {lthr} bpm >= max HR {max_hr} bpm")
        elif not low <= lthr / max_hr <= high:
            warn("lthr_implausible", f"{label} LTHR is {lthr / max_hr:.0%} of max HR (unusual)")

    age_max_hr = None
    if profile.athlete.birth_year is not None:
        age_max_hr = max_hr_from_age(today.year - profile.athlete.birth_year)

    def max_hr_or_estimate(field: str, value: int | None) -> int | None:
        if value is None and age_max_hr is not None:
            estimated.append(field)
            warn("estimated_max_hr", f"{field} estimated from age (208 - 0.7 * age): {age_max_hr}")
            return age_max_hr
        return value

    ftp = bike_lthr = bike_max = None
    bike = profile.thresholds.bike
    if bike is not None:
        ftp, bike_lthr = bike.ftp_w, bike.lthr_bpm
        if ftp is None and bike.ftp_20min_w is not None:
            ftp = ftp_from_20min(bike.ftp_20min_w)
            estimated.append("ftp_w")
            warn("estimated_ftp", f"ftp_w estimated as 0.95 x 20-min power: {ftp:.0f} W")
        bike_max = max_hr_or_estimate("bike_max_hr_bpm", bike.max_hr_bpm)
        check_test_date("bike FTP", bike.ftp_test_date)
        check_hr("bike", bike_lthr, bike_max)
        weight = profile.athlete.weight_kg
        if ftp is not None and weight is not None:
            low, high = PLAUSIBLE_W_PER_KG
            if not low <= ftp / weight <= high:
                warn("ftp_implausible", f"FTP/weight = {ftp / weight:.1f} W/kg looks implausible")

    pace = run_lthr = run_max = None
    run = profile.thresholds.run
    if run is not None:
        run_lthr = run.lthr_bpm
        race_pace = None
        if run.recent_race is not None:
            race = run.recent_race
            race_pace = threshold_pace_from_race(race.distance_km, race.time)
            low, high = RIEGEL_RELIABLE_RACE_S
            if not low <= race.time <= high:
                warn("race_extrapolation", "recent_race is far from 1 h; estimate less reliable")
        if run.threshold_pace is not None:
            pace = float(run.threshold_pace)
            if race_pace is not None and abs(race_pace - pace) / pace > 0.05:
                warn(
                    "pace_race_mismatch",
                    f"threshold_pace {format_clock(pace)} /km differs by more than 5% from the "
                    f"recent_race estimate {format_clock(race_pace)} /km",
                )
        elif race_pace is not None:
            pace = race_pace
            estimated.append("threshold_pace_s")
            warn(
                "estimated_threshold_pace",
                f"threshold_pace estimated from recent_race (Riegel): {format_clock(pace)} /km",
            )
        if pace is not None:
            low, high = PLAUSIBLE_THRESHOLD_PACE_S
            if not low <= pace <= high:
                warn("pace_implausible", f"threshold pace {format_clock(pace)} /km looks off")
        run_max = max_hr_or_estimate("run_max_hr_bpm", run.max_hr_bpm)
        check_test_date("run threshold", run.threshold_test_date)
        check_hr("run", run_lthr, run_max)

    resolved = ResolvedThresholds(
        ftp_w=ftp,
        bike_lthr_bpm=bike_lthr,
        bike_max_hr_bpm=bike_max,
        threshold_pace_s=pace,
        run_lthr_bpm=run_lthr,
        run_max_hr_bpm=run_max,
        estimated=tuple(estimated),
    )
    return resolved, issues
