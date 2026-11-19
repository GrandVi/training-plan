# endurance-planner

Generates structured, rule-based training plans that combine cycling (road, gravel, MTB)
and running, and pushes them to the [intervals.icu](https://intervals.icu) calendar as
structured workouts.

- One hand-edited `profile.yaml` (thresholds, habits, availability, goals) drives everything.
- Deterministic: the same profile and date always give the same plan. No randomness.
- Every rule is explicit, lives in a data table or a constant, and is checked by `validate_plan()`.
- A Python core library with a thin CLI on top (no logic in the CLI), ready for a future UI.

Supported goals: `run_5k`, `run_10k`, `run_half_marathon`, `mtb_xc`, `mtb_marathon`,
`ultra_endurance` (bike or run).

## Quick start

    pip install -e ".[dev]"

    cp examples/profile.example.yaml profile.yaml     # or: endurance-planner profile init
    endurance-planner profile validate                # checks the file, prints your zone tables

    endurance-planner plan generate -o plan.json -m plan.md
    endurance-planner plan show plan.json --week 3    # Markdown; -f csv / -f fitness-csv
    endurance-planner plan validate plan.json

    endurance-planner plan swap plan.json --date 2026-12-03   # bike <-> run, same intent
    endurance-planner plan retarget plan.json                 # after a new FTP / pace test

    export INTERVALS_API_KEY=...                       # or put it in a .env file (never in git)
    endurance-planner push plan.json --dry-run         # prints the payloads, no network
    endurance-planner push plan.json                   # idempotent: run it again to update
    endurance-planner clear plan.json                  # removes only what this tool created

All commands read `profile.yaml` in the current directory unless `-p` is given.
`examples/plan.example.md` shows the output for the example profile.

| Command | What it does |
|---|---|
| `profile init` | Interactive wizard, asks only for the required fields |
| `profile validate` | Validates the file, prints warnings and resolved zones (W, min/km, bpm) |
| `profile sync-intervals` | Compares thresholds with intervals.icu, offers to update the file |
| `profile schema` | Prints the JSON Schema of `profile.yaml` (`-o` to write it) |
| `plan generate` | Builds, validates and writes the plan (JSON, optional Markdown) |
| `plan show` | Markdown summary, weekly CSV, or daily CTL/ATL/TSB CSV |
| `plan validate` | Re-checks every rule, exit code 1 on errors |
| `plan swap` | Converts one session to the other sport and re-validates its week |
| `plan retarget` | Recomputes watts / paces / bpm, structure and dates untouched |
| `plan schema` | Prints the JSON Schema of `plan.json` |
| `push` | Upserts workouts to intervals.icu (`--dry-run`, `--from/--to`, `--absolute-targets`) |
| `clear` | Deletes this tool's events from intervals.icu (`--dry-run`, `--all-plans`) |

## Profile

    thresholds:
      bike: {ftp_w: 260, lthr_bpm: 168}
      run: {threshold_pace: "4:15", lthr_bpm: 172}
    current_fitness:
      weekly_hours: {bike: 6, run: 2}
      longest_recent_session_min: {bike: 180, run: 75}
    availability:
      days_per_week: 5
      weekly_hours_budget: 8
      weekdays: {mon: rest, tue: {bike: 60, run: 60}, sat: {bike: 240, run: 120}, ...}
      long_session_day: sat
      exceptions:                                     # optional, see "Calendar exceptions"
        - {from: 2027-01-04, to: 2027-01-10, mode: off, note: "ski week"}
    goals:
      - {name: "10k local race", date: 2027-03-14, type: run_10k, priority: A, target: {time: "41:00"}}

The complete, commented file is `examples/profile.example.yaml`. Unknown keys are rejected.
Thresholds can be estimated (`ftp_20min_w`, `recent_race`); estimates, stale tests (more than
8 weeks old) and inconsistent values are reported as warnings.

### Calendar exceptions

Dates known in advance are written once in `availability.exceptions` and used every time
the plan is generated or replanned:

    exceptions:
      - {from: 2027-01-04, to: 2027-01-10, mode: off, note: "ski week"}
      - {from: 2027-02-16, mode: limited, minutes: {bike: 45, run: 0}, note: "business trip"}
      - {from: 2027-03-02, mode: limited, minutes: {run: 0}, bike_activity: mtb}
      - {from: 2027-03-13, mode: limited, minutes: {bike: 300}, note: "free Saturday"}

- `to` is optional (one day); both ends are inclusive; ranges must not overlap; ranges
  before the plan start are ignored.
- `off`: no training. `limited`: `minutes` replaces the day's minutes for the sports listed,
  lower or higher than usual; sports not listed keep their usual minutes; 0 means "not that
  sport", so the session goes to the other sport with the usual run/bike conversion.
  "MTB instead of the run" is `run: 0` with `bike_activity: mtb`.
- `bike_activity: road | mtb | trainer` forces the kind of every bike session in the range.
- Phases and goal dates never move; only the sessions change. A raised day does not bypass
  the ramp caps (the free Saturday above gives a long ride of +10% at most), and a reduced
  week is never compensated later.
- After 7 or more consecutive days without training the first week back is capped at 85% of
  the reference load (highest of the last 4 weeks); after 14 or more days, 70% then 85%.
- An A goal inside an `off` range is an error; an exception in the 10 days before an A race
  is a warning. `plan show` marks the weeks with an exception (`*`).

**Editor support.** The first line of a profile names its JSON Schema:

    # yaml-language-server: $schema=../schemas/profile.schema.json

Editors using the YAML language server (VS Code YAML extension, Neovim, JetBrains) then
validate the file and complete its keys while you type. `profile init` writes this line with
the schema's URL on GitHub; to work offline or to match an older installed version, run
`endurance-planner profile schema -o profile.schema.json` and point the line at that file.

## How a plan is built

1. **Thresholds** (`thresholds.py`): resolve FTP, threshold pace, LTHR; estimate what is missing.
2. **Periodization** (`periodization.py`): from each main goal backwards: Base, Build, Peak,
   Taper, with recovery weeks every 4th week (`3:1`) or 3rd week (`2:1`). The best priority
   present (A) drives the macrocycles; two A goals are chained with a transition week;
   other goals get a mini-taper inside the plan. A first goal that is too close is refused
   with the earliest feasible date.
3. **Scheduler** (`scheduler.py`), week by week:
   - place sport-agnostic slots on days: `long`, `key_1`, `key_2`, `easy` (and `long_2` for ultras);
   - give each slot a sport: the goal sport first, the other sport as aerobic support;
   - pick key sessions from the interval library (`templates.py`) according to the goal and
     phase (`specificity.py`), one rung up the ladder each loading week;
   - size sessions to the week's volume, then lower the week (less volume, lower rungs, fewer
     keys) until it is under the load-ramp cap and the intensity limit.
4. **Targets** (`targets.py`): every step keeps its relative target (% of threshold) and gets
   absolute values (W, min/km, bpm).
5. **Validation** (`validation.py`): the finished plan is checked against every rule;
   generation fails loudly if one is broken.

## Rules and scientific rationale

| Principle | Rule implemented | Where to tune |
|---|---|---|
| Intensity distribution (polarized / pyramidal, Seiler) | Time above LT1 is capped at the target high share +5% when planning, error above +10% (week) or +5% (mesocycle). Measured by time in zone over both sports. | `preferences.intensity_ratio`, `rules.py` |
| Periodization | Base 45%, Build 35%, Peak 20% of the pre-taper weeks (ultra: 50/35/15) | `specificity.py` |
| Recovery weeks | Every 4th or 3rd week, volume x 0.65, load at most 80% of the previous week | `mesocycle_pattern`, `limits.recovery_week_factor` |
| Progressive overload | Impact-weighted load at most +8% over the highest of the last 4 weeks; planned ramp 4 / 6 / 7% by experience | `limits.max_weekly_ramp`, `scheduler.PLANNED_RAMP` |
| Run volume | At most +10% (or +10 min) a week over the last 4 weeks' peak | `limits.max_run_volume_increase` |
| Long session | Longest session per sport at most +10% (or +5 min) a week, up to a goal-specific ceiling | `limits.max_long_session_increase`, `specificity.py` |
| Starting load | Week 1 is at most the current load (from `current_fitness`), long session at most the longest recent one | `rules.BASELINE_IF` |
| Hard sessions | At most 2 a week (3 only in peak when `max_hard_sessions_per_week: 3`), at least 48 h apart across both sports, never the day after the long session if avoidable | `preferences`, `rules.py` |
| Return from a break | After 7+ days without training (calendar exceptions), the first week back is at most 85% of the reference load; after 14+ days, 70% then 85% | `rules.RETURN_FROM_BREAK` |
| Calendar exceptions | No session on an unavailable date or above that date's minutes; forced bike activity respected | `availability.exceptions` |
| Run impact | Run TSS x 1.25 (x 1.40 with injury notes) for ramp limits only; no hard or long run on consecutive days; run frequency cap | `limits`, `swap_rules.max_runs_per_week` |
| Taper (Mujika & Padilla; Bosquet et al.) | Volume x 0.60 for one week (5k, 10k, XC) or x 0.75 then x 0.55 (half, MTB marathon, ultra); intensity kept with short openers | `specificity.py` |
| Specificity | 10k: threshold, race-pace and VO2max runs, bike as support. MTB: threshold, VO2max, punches, race starts, skills rides. Ultra: one key a week, back-to-back long days, fueling notes, no VO2max block | `specificity.py` |
| Individual anchors | Coggan power zones, Daniels-style pace zones, Friel LTHR zones, or custom zones | `thresholds.zone_models` |

Each interval template carries its phases, progression ladder, prerequisites and a one-line
physiological rationale; each session carries a `rationale` ("why this session, why now").

### Load model

- Session load: `TSS = hours x IF^2 x 100` summed over steps. IF is the fraction of FTP
  (bike) or of threshold speed (run, rTSS-like). HR and RPE targets are mapped to an IF with
  the tables in `zones.py`.
- Weekly load for the ramp limits: bike TSS + run TSS x run impact multiplier. Displayed TSS
  is never weighted.
- `plan show -f fitness-csv` projects CTL / ATL / TSB (42 / 7 day exponential averages).

### Swap rules (`swap.py`)

A swap keeps the physiological intent and the impact-weighted load of every step:

- intensity is mapped through heart-rate equivalence (same % of LTHR): 95-100% FTP becomes
  96-100% of threshold speed, 60-72% FTP becomes 76-82%;
- duration is scaled so that `run TSS x multiplier = bike TSS`: 3 x 12 min at FTP becomes
  about 3 x 10 min at run threshold, a 60 min easy ride becomes a 34 min easy run;
- bike to run: blocks made only of efforts (over-unders) become one continuous cruise block;
- warm-up and cool-down take the destination sport's standard intensity and length;
- a technical session cannot keep its intent: it becomes plain endurance, with a warning;
- the result is fitted to the day's minutes for the new sport (shortened with a warning),
  then the plan is re-validated: `plan swap` refuses a swap that breaks a rule unless `--force`.

The scheduler uses the same conversion when a key run must be done on the bike because the
run-volume cap leaves no room for it.

## intervals.icu integration

- Auth: API key, HTTP Basic with username `API_KEY`. The key is read from `INTERVALS_API_KEY`
  (environment or `.env`), the athlete from `INTERVALS_ATHLETE_ID` (default `0` = the key's athlete).
- Workouts are calendar events (`category: WORKOUT`, `type` Ride / Run / MountainBikeRide)
  whose `description` uses the intervals.icu workout text syntax. Example:

      Intervals at FTP extend time at lactate threshold and raise sustainable power.

      - Warm-up 15m 50-70%

      3x
      - 12m 95-100%
      - Recovery 5m 45-55%

      - Cool-down 10m 45-55%

- Targets are **relative by default** (`95-100%`, `98-100% Pace`, `90-95% LTHR`), so workouts
  stay right when thresholds change in intervals.icu. Before pushing, the profile is compared
  with the intervals.icu settings and differences are printed. `--absolute-targets` pushes
  watts (`247-260w`) and paces (`4:15-4:20/km Pace`) instead.
- **Idempotent**: each event has `external_id = endurance-planner:<plan id>:<date>:<slot>` and
  is upserted (`POST /athlete/{id}/events/bulk?upsert=true`). Events of the same plan that
  are no longer in it are removed in the pushed window (`--no-prune` to keep them).
- `clear` lists the calendar window, keeps only events whose `external_id` has this tool's
  prefix, and deletes those (`PUT /athlete/{id}/events/bulk-delete`).
- Timeouts, 429 and 5xx are retried with exponential backoff; 401 / 403 / 404 / 422 give an
  explicit message; `--verbose` logs every request and response.

The API usage was checked against the intervals.icu forum guides ("Uploading planned workouts
to Intervals.icu", "Workout Builder Syntax Quick Guide", "API access to Intervals.icu").
Three things the text syntax cannot express, and what is pushed instead:

- RPE targets (hill repeats): the equivalent % of threshold, with the RPE in the description;
- nested repeats (over-unders, 30/15 sets): flattened to one level;
- absolute heart rate: HR targets stay in % LTHR even with `--absolute-targets`.

After the first push, open one workout of each kind in intervals.icu and check that the
chart matches; the tests use mocked HTTP only, no real call was made to the service.
Zwift / FIT files are not generated here: intervals.icu can export them from the pushed workouts.

## Limits

- The plan is not adapted to what you actually do. Regenerate it (same start date keeps the
  ids) when your availability or fitness changes; `adapt.py` documents the future hook.
- One session per day; no double days, no strength work, no swimming.
- Races are not sessions: the race day is kept free and its load is not counted.
- Load is planned from targets, not measured: real TSS will differ, mostly on variable terrain.
- By time in zone, plans land around 85-95% low intensity: the 20% is used as a ceiling,
  not as a quota.
- Key sessions must fit the minutes of their day. With 60 min on trainer days the ladders
  stop early; the plan says so (`key_sessions_capped_by_day`).
- Running power and elevation are ignored; `goal.target.elevation_m` is informative only.
- Nutrition is limited to short practice notes on long sessions.

## Assumptions

Defaults picked where the spec left a choice. All are data or constants that can be tuned.

Model:
- **Run intensity is a fraction of threshold speed**: 105% is 5% faster than threshold pace.
  This is also how intervals.icu defines `% Pace` (its threshold pace is stored as a speed).
- **Planned TSS** has no normalized-power smoothing and no grade adjustment.
- **Zone models**: zones are contiguous with inclusive upper bounds. `daniels_5` is a contiguous
  E/M/T/I/R approximation relative to T pace. `lthr_5` is Friel's zones with 5a-5c merged.
- **Low / high split**: a step is low when its target midpoint is at or below the top of the
  low zones (75% FTP, 84% of threshold speed, 89% LTHR, RPE 4). Recoveries count as low,
  tempo and sweet spot as high.
- **HR and RPE mappings** are piecewise-linear tables anchored on zone boundaries. No HR guide
  above 106% of threshold. Target mode `hr` falls back to RPE for such efforts.
- **Estimates**: FTP = 0.95 x 20-min power; threshold pace = Riegel pace of a 60-min race;
  max HR = 208 - 0.7 x age. Threshold inconsistencies are warnings, not errors.

Planning:
- The plan starts on the first Monday on or after today (`--start` to change).
- `preferences.primary_sport` decides the sport of easy sessions in a run plan and is the
  default sport of an ultra; the goal's sport decides key and long sessions.
- In a bike plan, running keeps the current weekly run volume (at most 30% of the week) as
  easy runs, on the days with the least bike time, and one easy slot stays a ride.
- In a run plan, key and long sessions are runs while the run-volume cap allows; a key run
  that does not fit is done on the bike (converted), and the long run keeps at least 75% of
  its length when key runs compete for volume.
- Ramp caps compare a week with the highest of the previous 4 weeks, so the week after a
  recovery week is compared with the last loading weeks. Small absolute steps are always
  allowed (15 TSS, 10 min of running, 5 min of long session) and a beginner starts from at
  least 2 h a week and 40 min of running.
- Week 1 volume is `min(budget, current hours)`, 10% less with under 4 weeks of consistency.
- Volume holds in peak; the long session and the key ladders keep progressing.
- Goal pace comes from `target.time`, clamped to +/-5% of threshold speed around the usual
  pace for the distance (5k 107%, 10k 103%, half 98%).
- MTB plans: one easy outdoor ride a week is flagged `technical`; long, technical and punch
  sessions use `MountainBikeRide` unless on an indoor trainer day.
- After a main race: one transition week at half volume, then training resumes at 85%.
- Distance-based reps keep `distance_m`; their duration is re-estimated on retargeting.
- A weekday missing from `availability.weekdays` is a rest day.

Calendar exceptions:
- Every availability lookup (scheduler, validation, swap, feasibility warning) goes through
  `Availability.minutes(date, sport)`; trainer and MTB choices go through
  `Availability.bike_activity(date)`. Without exceptions both return the weekly pattern, so
  existing plans are unchanged (checked by `tests/test_regression.py`).
- A "day without training" is a day on which no sport has minutes. Without exceptions this
  never lasts 7 days, so the return-from-break rule only follows exceptions. Days before the
  plan start do not count. The first week back is the week of the first possible training
  day after the break.
- A recovery week that is also the first week back is compared with the weeks before the
  break (the break was the recovery), not with the empty week before it.
- In a week with an exception, the long session goes to the day with the most time for the
  goal sport (the usual day on a tie, then the closest one), so a lost or limited long day
  moves the long session and a free day attracts it.
- `bike_activity: trainer` makes every bike session indoor, long ones included (the weekly
  `indoor_trainer_days` keep long sessions outdoors); `road` and `trainer` days get no MTB
  skills session. A key run moved to the bike because running is not possible says so in
  its rationale.
- `limited` ranges on an A race date are accepted (only `off` is refused): the race day has
  no session anyway.
- The `long_day_below_goal_needs` warning uses the most minutes the long-session day offers
  during the goal's weeks.
- A sport counts as available if the weekly pattern or an exception gives it minutes.

Tooling:
- `mypy` checks against the running interpreter (no pinned `python_version`); `ruff` targets 3.11.
- CI (GitHub Actions) runs `pytest`, `ruff check`, `ruff format --check` and `mypy` on
  Python 3.11 and 3.12.
- Plan files carry `schema_version` (now 2). Version 1 files are upgraded in memory when they
  are read (new fields take their defaults) and written back as version 2; newer versions are
  refused with a message asking to update the tool.
- The JSON Schemas are committed in `schemas/` (a test keeps them in sync with the models).
  The profile schema is the input form (paces as `"m:ss"`, `rest` days); the plan schema is the
  form the tool writes. Generated profiles point at the schema's URL on the `main` branch.
- The property-style tests use a seeded `random.Random` (150 profiles) rather than Hypothesis,
  to keep dependencies minimal.

## How to extend

- **New workout**: add a `Template` in `templates.py` (ladder, phases, rationale), then list
  its id in a rotation of `specificity.py`. `tests/test_templates.py` checks ladders and references.
- **New goal type**: add it to `GoalType`, to `GOAL_SPORT` if its sport is fixed, and a
  `GoalSpec` in `specificity.py`. The property test then covers it automatically.
- **New rule**: put the numbers in `rules.py`, a check in `validation.py` (returning an
  `Issue`), and make the scheduler respect it; add a tampering case in `tests/test_validation.py`.
- **Calendar rule**: read availability only through `Availability.minutes(date, sport)` and
  `Availability.bike_activity(date)`, so exceptions apply everywhere; break caps are in
  `rules.RETURN_FROM_BREAK`.
- **Tuning**: athlete-level caps are in `preferences.limits`; scheduling constants are at the
  top of `scheduler.py`; zone tables and HR / RPE mappings are in `zones.py`.
- **Plan file format**: add new plan fields as optional with a default that describes older
  files, bump `PLAN_SCHEMA_VERSION` in `models/plan.py`, extend `migrate_plan_data`, add a
  stored old file under `tests/fixtures/` and regenerate `schemas/` (`plan schema -o ...`).
- **Front-end**: call `planner.generate_plan`, `swap.swap_session`, `planner.retarget_plan`,
  `validation.validate_plan` and the `export` functions directly; the plan is a pydantic model
  (`Plan > Week > Session > Step | Repeat`) with a stable JSON form.
- **Adaptation from completed activities**: see `adapt.py`; the scheduler already builds each
  week from a history of weeks, so real weeks can replace planned ones.

## Development

    pip install -e ".[dev]"
    pytest && ruff check . && ruff format --check . && mypy

Tests never touch the network. `tests/test_regression.py` compares plans with fixtures made
before schema v2 (the example plan and a digest of each property-test plan), so a change of
behaviour shows up as a failing test. Layout:

    src/endurance_planner/
      models/          profile, plan, enums, issues (pydantic)
      config.py        profile loading          thresholds.py   threshold resolution
      zones.py         zone models and tables   targets.py      relative -> absolute targets
      load.py          TSS, low/high split, CTL/ATL/TSB
      rules.py         numeric caps shared by scheduler and validator
      periodization.py templates.py specificity.py scheduler.py planner.py
      swap.py          validation.py            adapt.py (future hook)
      export/          json_io, markdown (+ CSV), intervals_icu
      schemas.py       JSON Schemas (committed copies in schemas/)
      cli.py           Typer commands

## Disclaimer

This tool generates generic training plans from published rules of thumb. It is not medical
advice and does not replace a coach or a physician. Check with a doctor before starting a
training program, and stop when something hurts.
