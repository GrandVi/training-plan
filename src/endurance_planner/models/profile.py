"""Athlete profile schema (profile.yaml).

Field documentation lives in examples/profile.example.yaml. Unknown keys are
rejected so that typos in a hand-edited file fail loudly.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from endurance_planner.models.enums import (
    BikeActivity,
    Experience,
    GoalType,
    Priority,
    Sport,
    Weekday,
)
from endurance_planner.units import format_clock, parse_clock

# 'm:ss' / 'h:mm:ss' in YAML, seconds in memory.
Clock = Annotated[
    int,
    BeforeValidator(parse_clock),
    PlainSerializer(format_clock, return_type=str),
    Field(gt=0),
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "string", "pattern": r"^\d+:\d{2}(:\d{2})?$"},
                {"type": "integer", "exclusiveMinimum": 0},
            ],
            "description": "'m:ss' or 'h:mm:ss' (quoted), or seconds",
        }
    ),
]
Bpm = Annotated[int, Field(gt=60, lt=240)]
DayMinutes = Annotated[int, Field(ge=0, le=24 * 60)]
WEEKDAYS = list(Weekday)

# Sport of each goal type; ultra_endurance uses goal.sport or preferences.primary_sport.
GOAL_SPORT: dict[GoalType, Sport] = {
    GoalType.RUN_5K: Sport.RUN,
    GoalType.RUN_10K: Sport.RUN,
    GoalType.RUN_HALF_MARATHON: Sport.RUN,
    GoalType.MTB_XC: Sport.BIKE,
    GoalType.MTB_MARATHON: Sport.BIKE,
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Athlete(_Strict):
    name: str
    birth_year: int | None = Field(None, ge=1920, le=2020)
    weight_kg: float | None = Field(None, gt=30, lt=200)
    experience: Experience = Experience.INTERMEDIATE
    injury_notes: str = ""


class BikeThresholds(_Strict):
    ftp_w: float | None = Field(None, gt=50, lt=600)
    ftp_20min_w: float | None = Field(None, gt=50, lt=650)
    lthr_bpm: Bpm | None = None
    max_hr_bpm: Bpm | None = None
    ftp_test_date: dt.date | None = None


class RecentRace(_Strict):
    distance_km: float = Field(gt=0)
    time: Clock


class RunThresholds(_Strict):
    threshold_pace: Clock | None = None
    lthr_bpm: Bpm | None = None
    max_hr_bpm: Bpm | None = None
    recent_race: RecentRace | None = None
    threshold_test_date: dt.date | None = None


class ZoneDef(_Strict):
    name: str
    # Inclusive upper bound as a fraction of the threshold anchor; omitted for the top zone.
    upper: float | None = Field(None, gt=0)


class ZoneModel(_Strict):
    """Contiguous zones defined by their inclusive upper bounds."""

    zones: list[ZoneDef] = Field(min_length=2)
    # Number of zones (from the bottom) counted as low intensity, i.e. below LT1/VT1.
    low_zones: int = Field(ge=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        uppers = [z.upper for z in self.zones]
        if uppers[-1] is not None or any(u is None for u in uppers[:-1]):
            raise ValueError("only the last zone may (and must) omit 'upper'")
        bounded = [u for u in uppers if u is not None]
        if bounded != sorted(set(bounded)):
            raise ValueError("zone upper bounds must be strictly increasing")
        if self.low_zones >= len(self.zones):
            raise ValueError("low_zones must be smaller than the number of zones")
        return self


class ZoneModels(_Strict):
    bike_power: Literal["coggan_7", "custom"] = "coggan_7"
    run_pace: Literal["daniels_5", "custom"] = "daniels_5"
    hr: Literal["lthr_5", "custom"] = "lthr_5"
    custom: dict[Literal["bike_power", "run_pace", "hr"], ZoneModel] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> Self:
        for key in ("bike_power", "run_pace", "hr"):
            if getattr(self, key) == "custom" and key not in self.custom:
                raise ValueError(f"{key} is 'custom' but zone_models.custom.{key} is missing")
        return self


class TargetMode(_Strict):
    bike: Literal["power", "hr", "rpe"] = "power"
    run: Literal["pace", "hr", "rpe"] = "pace"


class Thresholds(_Strict):
    bike: BikeThresholds | None = None
    run: RunThresholds | None = None
    zone_models: ZoneModels = Field(default_factory=ZoneModels)
    target_mode: TargetMode = Field(default_factory=TargetMode)

    def anchored(self, sport: Sport) -> bool:
        """True if targets of this sport can be resolved to absolute values."""
        if sport is Sport.BIKE:
            bike = self.bike
            match self.target_mode.bike:
                case "power":
                    return bike is not None and (bike.ftp_w or bike.ftp_20min_w) is not None
                case "hr":
                    return bike is not None and bike.lthr_bpm is not None
                case _:
                    return True
        run = self.run
        match self.target_mode.run:
            case "pace":
                return run is not None and (run.threshold_pace or run.recent_race) is not None
            case "hr":
                return run is not None and run.lthr_bpm is not None
            case _:
                return True


class SportHours(_Strict):
    bike: float = Field(0, ge=0)
    run: float = Field(0, ge=0)

    def of(self, sport: Sport) -> float:
        return self.bike if sport is Sport.BIKE else self.run


class SportMinutes(_Strict):
    bike: int = Field(0, ge=0)
    run: int = Field(0, ge=0)

    def of(self, sport: Sport) -> int:
        return self.bike if sport is Sport.BIKE else self.run


class CurrentFitness(_Strict):
    weekly_hours: SportHours
    longest_recent_session_min: SportMinutes = Field(default_factory=SportMinutes)
    weeks_of_consistent_training: int = Field(4, ge=0)


class SwapRules(_Strict):
    allow_run_instead_of_bike: bool = True
    allow_bike_instead_of_run: bool = True
    max_runs_per_week: int = Field(3, ge=0, le=7)
    no_hard_run_after_hard_bike: bool = True


def _rest_day_schema(schema: dict[str, Any]) -> None:
    """JSON Schema of `weekdays`: a day is minutes per sport, "rest" or empty."""
    minutes = schema.pop("additionalProperties")
    schema["additionalProperties"] = {"anyOf": [minutes, {"const": "rest"}, {"type": "null"}]}


class CalendarException(_Strict):
    """A date range planned in advance: no training (off) or other minutes (limited).

    `from` and `to` are inclusive; without `to` the range is one day. With `limited`,
    `minutes` sets the minutes of the sports listed (lower or higher than usual, 0 = not
    available); sports not listed keep their usual minutes. `bike_activity` forces the
    kind of every bike session in the range.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    start: dt.date = Field(alias="from")
    end: dt.date | None = Field(None, alias="to")
    mode: Literal["off", "limited"]
    minutes: dict[Sport, DayMinutes] = Field(default_factory=dict)
    bike_activity: BikeActivity | None = None
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.end is not None and self.end < self.start:
            raise ValueError(f"exception {self.start}: 'to' ({self.end}) is before 'from'")
        where = f"exception {self.start}"
        if self.mode == "off" and (self.minutes or self.bike_activity):
            raise ValueError(f"{where}: mode 'off' takes no minutes or bike_activity")
        if self.mode == "limited" and not (self.minutes or self.bike_activity):
            raise ValueError(f"{where}: mode 'limited' needs minutes or bike_activity")
        return self

    @property
    def last(self) -> dt.date:
        return self.end or self.start

    def covers(self, date: dt.date) -> bool:
        return self.start <= date <= self.last

    def overlaps(self, first: dt.date, last: dt.date) -> bool:
        return self.start <= last and first <= self.last

    def label(self) -> str:
        dates = str(self.start) if self.last == self.start else f"{self.start} to {self.last}"
        details: list[str] = [self.mode]
        details += [f"{sport} {minutes} min" for sport, minutes in sorted(self.minutes.items())]
        if self.bike_activity:
            details.append(f"bike: {self.bike_activity}")
        note = f" ({self.note})" if self.note else ""
        return f"{dates} {', '.join(details)}{note}"


class Availability(_Strict):
    days_per_week: int = Field(ge=1, le=7)
    weekly_hours_budget: float = Field(gt=0)
    # Max minutes per sport for each day; "rest" (or a missing day) means no training.
    weekdays: dict[Weekday, SportMinutes] = Field(json_schema_extra=_rest_day_schema)
    long_session_day: Weekday = Weekday.SAT
    indoor_trainer_days: list[Weekday] = Field(default_factory=list)
    swap_rules: SwapRules = Field(default_factory=SwapRules)
    exceptions: list[CalendarException] = Field(default_factory=list)

    @field_validator("weekdays", mode="before")
    @classmethod
    def _expand_rest_days(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        days: dict[str, Any] = {day.value: {} for day in Weekday}
        for key, minutes in value.items():
            days[str(key)] = {} if minutes in ("rest", None) else minutes
        return days

    @model_validator(mode="after")
    def _check(self) -> Self:
        training_days = self.training_days()
        if self.days_per_week > len(training_days):
            raise ValueError(
                f"days_per_week={self.days_per_week} but only {len(training_days)} "
                "weekdays have training time"
            )
        if self.long_session_day not in training_days:
            raise ValueError(f"long_session_day '{self.long_session_day}' is a rest day")
        for day in self.indoor_trainer_days:
            if not self.weekdays[day].bike:
                raise ValueError(f"indoor_trainer_days: '{day}' has no bike time")
        ranges = sorted(self.exceptions, key=lambda exc: exc.start)
        for first, second in zip(ranges, ranges[1:], strict=False):
            if second.start <= first.last:
                raise ValueError(
                    f"exceptions overlap: {first.start}..{first.last} and "
                    f"{second.start}..{second.last}"
                )
        return self

    def training_days(self) -> list[Weekday]:
        return [day for day in Weekday if self.weekdays[day].bike or self.weekdays[day].run]

    def exception_on(self, date: dt.date) -> CalendarException | None:
        """Calendar exception covering `date` (the last listed wins if several do)."""
        found = None
        for exception in self.exceptions:
            if exception.covers(date):
                found = exception
        return found

    def minutes(self, date: dt.date, sport: Sport) -> int:
        """Max minutes of `sport` on `date`: the weekly pattern, unless an exception applies.

        This is the only availability lookup used to plan and to check a plan.
        """
        usual = self.weekdays[WEEKDAYS[date.weekday()]].of(sport)
        exception = self.exception_on(date)
        if exception is None:
            return usual
        if exception.mode == "off":
            return 0
        return exception.minutes.get(sport, usual)

    def forced_bike_activity(self, date: dt.date) -> BikeActivity | None:
        """Kind of bike session imposed on `date` by an exception."""
        exception = self.exception_on(date)
        return exception.bike_activity if exception is not None else None

    def bike_activity(self, date: dt.date) -> BikeActivity | None:
        """Kind of bike session on `date`: forced by an exception, else trainer on
        indoor_trainer_days, else None (the planner chooses road or MTB)."""
        forced = self.forced_bike_activity(date)
        if forced is not None:
            return forced
        indoor = WEEKDAYS[date.weekday()] in self.indoor_trainer_days
        return BikeActivity.TRAINER if indoor else None

    def has_time(self, sport: Sport) -> bool:
        """True if some day of the weekly pattern or some exception has time for `sport`."""
        if any(day.of(sport) for day in self.weekdays.values()):
            return True
        return any(exception.minutes.get(sport, 0) > 0 for exception in self.exceptions)


class GoalTarget(_Strict):
    time: Clock | None = None
    duration_h: float | None = Field(None, gt=0)
    distance_km: float | None = Field(None, gt=0)
    elevation_m: float | None = Field(None, ge=0)


class Goal(_Strict):
    name: str
    date: dt.date
    type: GoalType
    priority: Priority = Priority.A
    sport: Sport | None = None  # only meaningful for ultra_endurance
    target: GoalTarget = Field(default_factory=GoalTarget)

    @model_validator(mode="after")
    def _check(self) -> Self:
        fixed = GOAL_SPORT.get(self.type)
        if fixed and self.sport and self.sport is not fixed:
            raise ValueError(f"goal type '{self.type}' is a {fixed} goal, got sport='{self.sport}'")
        return self


class IntensityRatio(_Strict):
    low: float = Field(0.80, gt=0, lt=1)
    high: float = Field(0.20, gt=0, lt=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if abs(self.low + self.high - 1) > 1e-6:
            raise ValueError("intensity_ratio low + high must sum to 1")
        return self


class Limits(_Strict):
    """Safety caps, all relative (0.08 = 8%)."""

    max_weekly_ramp: float = Field(0.08, gt=0, le=0.15)
    max_run_volume_increase: float = Field(0.10, gt=0, le=0.20)
    max_long_session_increase: float = Field(0.10, gt=0, le=0.25)
    recovery_week_factor: float = Field(0.65, ge=0.5, le=0.8)
    # Run TSS is multiplied by this for ramp/fatigue limits only (never for displayed TSS).
    run_impact_multiplier: float = Field(1.25, ge=1.0, le=2.0)
    # Added to run_impact_multiplier when athlete.injury_notes is non-empty.
    injury_run_impact_bonus: float = Field(0.15, ge=0, le=1.0)


class Preferences(_Strict):
    primary_sport: Sport = Sport.BIKE
    intensity_ratio: IntensityRatio = Field(default_factory=IntensityRatio)
    mesocycle_pattern: Literal["3:1", "2:1"] = "3:1"
    max_hard_sessions_per_week: int = Field(2, ge=1, le=3)
    limits: Limits = Field(default_factory=Limits)


class Profile(_Strict):
    athlete: Athlete
    thresholds: Thresholds
    current_fitness: CurrentFitness
    availability: Availability
    goals: list[Goal] = Field(min_length=1)
    preferences: Preferences = Field(default_factory=Preferences)

    @model_validator(mode="after")
    def _check(self) -> Self:
        self.goals.sort(key=lambda goal: goal.date)
        names = [goal.name for goal in self.goals]
        if len(set(names)) != len(names):
            raise ValueError("goal names must be unique")
        for goal in self.goals:
            exception = self.availability.exception_on(goal.date)
            if goal.priority is Priority.A and exception is not None and exception.mode == "off":
                raise ValueError(
                    f"A goal '{goal.name}' ({goal.date}) is inside the off range "
                    f"{exception.label()}"
                )
        needed = {self.preferences.primary_sport} | {self.goal_sport(g) for g in self.goals}
        for sport in sorted(needed):
            if not self.thresholds.anchored(sport):
                mode = getattr(self.thresholds.target_mode, sport.value)
                raise ValueError(
                    f"{sport} is the primary sport or a goal sport, but thresholds.{sport} has no "
                    f"anchor for target_mode '{mode}' (power: ftp_w or ftp_20min_w; "
                    "pace: threshold_pace or recent_race; hr: lthr_bpm)"
                )
        return self

    def goal_sport(self, goal: Goal) -> Sport:
        return goal.sport or GOAL_SPORT.get(goal.type, self.preferences.primary_sport)
