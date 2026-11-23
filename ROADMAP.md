# Roadmap: next features (M8 to M11)

Status: proposal v3, 2026-11-23 (strength training moved up). Baseline: M1 to M6, M7 (F1 to F3)
and the Portability work (service layer, API, web app) are delivered (commit `e2fb061`).

Working rules are unchanged: one commit per feature (a milestone groups several), `pytest`,
`ruff` and `mypy` green, deterministic generation, rules and templates as data, no logic in the
CLI, every default chosen is added to the README "Assumptions" section.

## Overview

Each feature closes a gap listed in the README "Limits" section, or a risk the README admits.

| # | Feature | Gap it closes | Size | Milestone |
|---|---|---|---|---|
| F1 | Foundations: CI, JSON Schema, plan schema v2 | No CI, no editor help, schema is v1 | S | M7 (done) |
| F2 | Planned calendar exceptions (known in advance) | Availability is one fixed weekly pattern | M | M7 (done) |
| F3 | Plan amendments and replanning (changes along the way) | An in-progress plan cannot be changed or regenerated | L | M7 (done) |
| F4 | Races on the calendar + race-day form check | "Races are not sessions" | S | M9 |
| F5 | Threshold tests + `profile record-test` | Stale thresholds only raise a warning | M | M9 |
| F6 | `doctor`: live check of the intervals.icu integration | "No real call was made to the service" | S | M10 |
| F7 | Activity sync + `plan review` (planned vs actual) | Load is planned, never measured | M | M10 |
| F8 | `plan adapt`: rebuild the remaining weeks from what was done | "The plan is not adapted to what you actually do" | M | M10 |
| F9 | New goal types + vertical-gain-aware long sessions | Elevation ignored; no marathon, trail or gran fondo | M | M11 |
| F10 | Strength sessions (equipment-aware, winter-ready) | "No strength work" | M | M8 |

Order: M7 and the Portability work are delivered. M8 (strength) comes next because winter is
the season where it helps most and where outdoor sessions are fewer. M9 is offline and
deterministic. M10 needs a real intervals.icu account and reuses the replanning engine built in
F3. M11 widens the goal types once the plan can follow reality.

---

## M7: A calendar that can change

Two kinds of change, one engine:
- **Known in advance**: written in `profile.yaml` (F2), used when the plan is generated, and
  also at any later `plan replan`.
- **Decided along the way**: a command on the running plan (F3), recorded in the plan, and the
  rest of the plan adapts.

### F1. Foundations

- GitHub Actions workflow: Python 3.11 and 3.12 running `pytest`, `ruff check`,
  `ruff format --check` and `mypy`. There is no `.github` folder today.
- `endurance-planner profile schema` and `plan schema` print the pydantic JSON Schema. The
  generated and example profiles get a `# yaml-language-server: $schema=...` header, so editors
  validate and autocomplete `profile.yaml`.
- `Plan.schema_version` becomes 2, with new optional fields used by F3 and later features:
  `Session.pinned`, `Session.rung`, `Session.amendment_id`, `Plan.amendments`. The loader
  accepts version 1 files and migrates them in memory; a test loads a stored v1 plan.

### F2. Planned calendar exceptions

**Why.** A holiday week or a trip means editing the whole weekly pattern, and the plan cannot
plan around it.

**What.** New optional field, default empty:

    availability:
      exceptions:
        - {from: 2027-01-04, to: 2027-01-10, mode: off, note: "ski week"}
        - {from: 2027-02-16, mode: limited, minutes: {bike: 45, run: 0}, note: "business trip"}
        - {from: 2027-03-02, mode: limited, minutes: {run: 0}, bike_activity: mtb}
        - {from: 2027-03-13, mode: limited, minutes: {bike: 300}, note: "free Saturday"}

- `to` is optional (a single day). Both ends are inclusive. Ranges must not overlap. Ranges
  before the plan start are ignored.
- `off`: no training on those days.
- `limited`: `minutes` sets the day's minutes for the sports listed, lower or higher than usual;
  sports not listed keep their usual minutes. A sport set to 0 is unavailable that day, so the
  scheduler gives the session to the other sport with its existing run/bike conversion: "MTB
  instead of the run" is `run: 0` plus `bike_activity: mtb`.
- `bike_activity: road | mtb | trainer` forces the activity type of the bike sessions in the range.

**Implementation note.** Availability is read by weekday in several places: `Scheduler._minutes`
and `_place`, the day check in `validation.py`, the limit in `swap.py`, and the long-day
feasibility warning in `planner.py`; `indoor_trainer_days` is read in three scheduler spots. Replace
the weekday lookups with one date-aware method, `Availability.minutes(date, sport)`, and route
indoor and MTB choices through `bike_activity`.

**Rules.**
- Phases and goal dates do not move (they come from the goals); only the sessions of the
  affected weeks change.
- Safety caps and the weekly budget still apply: a raised day does not bypass the ramp caps,
  the scheduler lowers the session. A reduced week is never compensated later.
- New return-from-break rule in `rules.py`: after 7 or more consecutive days without training,
  the first week back is at most 85% of the reference load; after 14 or more days, 70% then 85%.
  These are defaults to tune. F8 reuses the same function.
- Error if an A goal date falls inside an `off` range. Warning if an exception overlaps the
  last 10 days before an A race.

**Done when.**
- No session is ever planned on an unavailable date or above that date's minutes: new check
  `session_on_unavailable_date` in `validation.py`, with a tampering test.
- Scenarios: a week off; a trip day with 45 min and no run; a MTB-only day; a free long day.
- The seeded property tests (150 profiles) also draw random exceptions and every plan passes
  `validate_plan`. Without exceptions, every existing test and plan is unchanged.
- `plan show` marks the affected weeks; `examples/profile.example.yaml` documents the field.

### F3. Plan amendments and replanning

**Why.** Two gaps. First, `generate_plan` refuses a start date before today, so a plan in
progress cannot be regenerated (the README advice only works before the start). Second,
`plan swap` changes one session in isolation and only between bike and run: nothing shortens,
moves or skips a session, and nothing rebalances the rest of the plan.

**What.** Three parts.

1. **Replanning engine** (`replan.py`, library): `replan(plan, profile, today, history=None) -> Plan`.
   - Frozen: every session dated before today. The skeleton comes from
     `build_skeleton(profile, first week start)`; if it no longer matches the frozen weeks (a goal
     date edited after the start), replan stops with `skeleton_changed`.
   - The scheduler's progression state (`uses`, `rotation`, `level_s`) is seeded from the frozen
     weeks. `build_week` accepts pinned sessions: they are placed first, counted in load, run
     volume, long-session and spacing checks, and the rest of the week is built around them.
   - Same plan id. Sessions that did not change keep their `external_id`, so `push` only
     updates differences. The result lists every change with a reason code.
2. **Amendments** (`amend.py`, pure functions). An amendment edits one session and pins it
   (`Session.pinned`, `amendment_id`); a replan never touches a pinned session.
3. **Commands**:

       endurance-planner plan amend plan.json --date 2026-12-03 --minutes 40          # shorter
       endurance-planner plan amend plan.json --date 2026-12-05 --sport bike --activity mtb --minutes 90
                                                                                       # MTB instead of the run
       endurance-planner plan amend plan.json --date 2026-12-08 --sport run --minutes 50   # run instead of the ride
       endurance-planner plan amend plan.json --date 2026-12-10 --skip --reason "work trip"
       endurance-planner plan amend plan.json --date 2026-12-12 --move-to 2026-12-13
       endurance-planner plan amend plan.json --date 2026-12-14 --intent endurance     # make it easier
       endurance-planner plan unavailable plan.json --from 2026-12-20 --to 2026-12-27 --minutes run=0,bike=60
       endurance-planner plan replan plan.json                      # after editing profile.yaml
       endurance-planner plan amendments plan.json [--undo ID]

   Common options: `--scope session|week|rest` (default `rest`), `--dry-run` (prints the diff),
   `--reason TEXT`, `--force`. Flags combine, except `--skip`. `plan swap` stays as a shortcut
   for `--sport` set to the other sport. Nothing is ever pushed automatically.

**What "adapt the plan accordingly" means** (data table in `rules.py`, all tunable):
- **Scope**: `session` changes only that session and re-validates its week (like `swap` today);
  `week` also rebalances the unpinned sessions of that week; `rest` also rebuilds all later
  weeks, because ramp references and ladder progress change.
- **Shorter**: the session is fitted to the new minutes by scaling its sets down the ladder
  (reusing `swap.py` helpers `convert_session` and `_scale_blocks`). If the template cannot keep
  its intent, it becomes plain endurance with a warning, as `swap` does today.
- **Other sport or bike type**: same-intent, same impact-weighted load conversion as `swap`;
  `--activity road | mtb | trainer` sets the activity type. A technical session cannot keep its
  intent and becomes endurance with a warning.
- **No catch-up**: lost load is never made up in other sessions.
- **Rung hold**: a key session that is skipped, or shortened below 80% of its length, does not
  advance its ladder; the next one repeats the rung. This uses `Session.rung`.
- **Unpinned sessions yield**: if an amendment breaks the 48 h hard-session spacing or the weekly
  hard cap, the unpinned session moves to a free day, else is downgraded one rung, with reason
  code `yielded_to_amendment`.
- **Explicit means allowed**: an amendment may exceed the usual minutes of that day (you are
  stating you can), never the safety caps unless `--force`. With `--force` the plan is saved
  and the broken rules are reported, as `swap --force` does.
- **Dates**: amendments before today are refused (that is F8's job); a later amendment on the
  same date replaces the earlier one and both stay in the log.

**Storage.** Amendments and pins live in `plan.json`. `profile.yaml` stays hand-edited and is
never rewritten by these commands. A fresh `generate` starts clean; changes to a running plan go
through `replan`, `amend` and `unavailable`.

**Done when.**
- Scenarios: shorten a key session (week valid, rung held next time); MTB instead of the run
  (`MountainBikeRide`, run volume cap not used); run instead of a ride; skip a key (no
  catch-up); move a session next to a hard one (the other one yields, reason code present);
  unavailable range equals the same range written as an F2 exception.
- Property test: random sequences of 1 to 5 amendments on the 150 profiles always give a plan
  that passes `validate_plan` (forced amendments excepted, and reported).
- Applying an amendment twice equals once; `--undo` gives exactly the plan a replan without
  that amendment would give; `replan` twice gives the same plan.
- After an amendment, `push` with mocked HTTP updates only the changed events.
- Amendment and replan logic are pure library functions; the CLI only parses and prints.

---

## M8: Strength training

### F10. Strength sessions

**Why.** Strength work is standard in a running and MTB plan, is most practical in winter, and
is entirely absent today.

**What.**
- Profile (default 0 sessions keeps every current plan unchanged):

      availability:
        strength:
          sessions_per_week: 2          # maximum; 0 = off
          minutes: 40                   # 20 to 60
          allow_extra_days: false       # true = may use a rest day
          equipment: [bodyweight, dumbbells, barbell]   # this is the default

  Equipment values: `bodyweight` (always available), `dumbbells`, `barbell`, `kettlebell`,
  `bands`, `pullup_bar`, `machines`. The default list is bodyweight, dumbbells and barbell.
- F2 exceptions accept an optional `equipment` list for their dates (for example a trip with
  `equipment: [bodyweight]`), and `plan amend` can change the equipment of one session.
- Separate model `Week.strength: list[StrengthSession]` instead of a new `Sport`, so the many
  per-sport loops and the ramp logic stay untouched.
- Exercise library as data (`strength.py`): id, name, movement pattern (squat, hinge,
  single-leg, push, pull, calf, core), required equipment (any of), unilateral flag, and a
  one-line rationale. The scheduler picks, for each pattern, the first exercise whose equipment
  is available, so the same plan degrades cleanly to bodyweight. Barbell and dumbbell squats,
  Romanian deadlifts, split squats and step-ups, presses and rows are the core of the library;
  no jumps or plyometrics by default (run impact), and none at all when `athlete.injury_notes`
  is set.
- Prescription as sets x reps at a target reps in reserve (RIR), not a percentage of a 1RM the
  athlete never measured. Each template has a ladder like the interval templates:
  - Base: foundation, 2 to 3 sets of 10 to 15 reps at RIR 3 to 4, 2 sessions a week;
  - Build: heavier, 3 to 5 sets of 4 to 8 reps at RIR 1 to 3, 2 sessions a week;
  - Peak: maintenance, 1 session, 1 to 2 heavy sets per pattern;
  - Taper and transition: 1 light session, none in the last 3 days before an A race;
  - Recovery weeks: volume reduced by about 40%.
  A table in `strength.py` gives the sessions per phase; the profile value is a cap.
- Placement rules: on a training day after an easy endurance session, or on a rest day only if
  `allow_extra_days`; never the day before a hard or long session, and never on the day of one.
  Strength minutes count in `weekly_hours_budget` but not in the TSS ramp caps.
- Load: a flat estimate (`STRENGTH_TSS_PER_HOUR`, default 35, tunable) is added to the CTL/ATL/TSB
  projection and pushed as the event load, so the intervals.icu forecast is not distorted. It
  never enters the ramp caps.
- Export: intervals.icu type `WeightTraining` (new `ActivityType`), exercises as plain text in
  the description (the workout text syntax has no sets and reps), same idempotent
  `external_id` with a `strength` slot.
- Amendments (F3): shorten (accessory exercises are dropped first), skip, move, change equipment.
  A skipped or shortened-below-80% session holds its ladder rung.
- Services, API and web app: `show_week` and the week and session responses include strength
  sessions; the web week view shows them; the amend form works on them.

**Done when.**
- Placement, phase, equipment and race-proximity rules each have a validation check and a
  tampering test.
- The property tests draw `sessions_per_week` 0 to 3 and random equipment lists (bodyweight
  always present): every plan passes `validate_plan`, every exercise uses only available
  equipment, and with 0 sessions plans are identical to before.
- Winter scenario: a 12-week Base block with the default equipment gives 2 sessions a week, never
  before a hard or long session, none within 3 days of the A race.
- Mocked-HTTP tests cover the `WeightTraining` payload and idempotent upsert; the API and web
  tests cover display and amendment of a strength session.
- README: new section, commands, Assumptions; the "no strength work" limit is removed. The
  disclaimer stays: generic strength guidance, no injury advice.

---

## M9: Race readiness (offline)

### F4. Races on the calendar and race-day form

**Why.** Races are invisible in intervals.icu, and nothing checks that the athlete arrives fresh.

**What.**
- `push` creates one event per goal with category `RACE_A`, `RACE_B` or `RACE_C` (from the
  priority), the target in the description, and `external_id` `endurance-planner:<plan id>:<date>:race`.
  `--no-races` skips them; `clear` already removes them through the prefix.
- Estimated race load, `hours x IF^2 x 100`: hours from `target.time` or `duration_h` (else the
  goal-type default), IF from a per-goal-type table in `specificity.py`. It enters the
  CTL/ATL/TSB projection (`plan show -f fitness-csv`) but not the ramp caps, so races stay out
  of the weekly load as today.
- New check `race_form`: projected TSB on the morning of an A race should fall in a band
  (proposed +5 to +25). Warning only, it never blocks generation.

**Done when.**
- Mocked-HTTP tests cover the categories, the idempotent upsert and `clear`.
- The projection test uses hand-computed values; `race_form` has a tampering test.
- Before freezing the band, measure the TSB distribution over the property-test profiles. If
  many plans fall outside, adjust the taper constants in `specificity.py`, not the band.

### F5. Threshold tests and `profile record-test`

**Why.** The README warns when a test is older than 8 weeks, but the plan never schedules one,
and after a test the profile and the plan are updated by hand.

**What.**
- Two new templates: `bike_ftp_20min_test` and `run_threshold_test_30min` (a 30-minute time
  trial recorded as a `recent_race`, so the existing Riegel logic gives the threshold pace).
- Scheduling, opt-in: `preferences.threshold_tests: {enabled: false, every_weeks: 8}`. A test
  replaces a key session in the first loading week after a recovery week, never in peak or taper,
  never within 10 days of an A race. It counts as a hard session. It cannot be swapped or amended
  into another sport.
- `profile record-test --sport bike --power-20min 285` (or `--sport run --distance-km 7.4 --time 30:00`,
  plus optional `--lthr`): updates the profile scalars and the test date through the existing
  `update_yaml_scalars` (comments preserved), then retargets the plan with `--plan plan.json`.
  Relative targets never change, so no regeneration is needed. That helper only replaces keys
  that already have a value and returns the missing ones; extend it to insert a missing key
  (for example `ftp_test_date`, often commented out) under its existing parent mapping.

**Done when.**
- Property tests show tests only in allowed weeks; `plan validate` flags one in a taper week.
- A golden-file test shows `record-test` changes only the intended lines.
- Default off keeps every existing plan unchanged; the example profile turns it on.

---

## M10: Closing the loop with intervals.icu

### F6. `doctor`: live check of the integration

**Why.** Tests use mocked HTTP only; the README asks the user to check the first push by hand.

**What.** `endurance-planner doctor`:
1. reads the athlete and sport settings (auth check);
2. pushes one workout of each family (bike power, bike HR, run pace, run RPE, MTB, nested
   repeats flattened) and one race event, under the prefix `endurance-planner:doctor:`, in a
   scratch window;
3. reads them back and compares planned duration and TSS with what the event reports
   (`icu_training_load`, returned for planned workouts); drift above 10% is a warning;
4. upserts again and checks that no duplicate appears;
5. deletes everything it created, in a `finally` block, then lists the window to prove it;
6. prints an OK, WARN or FAIL table.

It also confirms the assumptions F4 and F8 rely on: the `RACE_A/B/C` categories, and whether the
calendar offers `SICK`, `INJURED` and `HOLIDAY` events.

Optional: `push --pin-load` overrides `icu_training_load` with the planner's TSS (the API accepts
a PUT of this field) so the calendar forecast matches the plan.

**Done when.** The report logic has mocked-HTTP unit tests; one `@pytest.mark.live` test runs
only when an API key is set; the README integration section replaces the "not verified"
caveat with the observed behavior. The user runs `doctor` once on their own account.

### F7. Activity sync and `plan review`

**Why.** Nothing reads what was actually done, so the load model is never checked.

**What.**
- `IntervalsClient.list_activities` and `list_wellness` (the `/activities` and `/wellness`
  endpoints already named in `adapt.py`).
- `endurance-planner sync` writes `history.json`: one `DayObservation` per activity (extend the
  existing dataclass with `activity_id`, `intensity_factor`, optional time in zones). Idempotent by
  activity id. Ride, VirtualRide and MountainBikeRide map to bike; Run and TrailRun map to run;
  any other type is counted and reported, not silently dropped.
- `endurance-planner plan review plan.json [--week N | --since DATE]`: per week, planned vs
  actual duration and TSS, completion rate of each key session (matched by date and sport),
  missed keys, hard-session count, real low/high intensity share. Markdown and CSV, from a pure
  `review.py`.

**Done when.** Recorded JSON fixtures (no network) reproduce hand-computed totals; running
`sync` twice changes nothing; unknown sport types appear in the report.

### F8. `plan adapt`

**Why.** The plan still ignores what really happened. With F3, this becomes `replan` fed with
observed history instead of the planned past.

**What.** `endurance-planner plan adapt plan.json --history history.json -o plan.adapted.json [--dry-run]`:
- past weeks become history: each completed activity is a synthetic `Session` (one step whose IF
  comes from actual TSS and duration, `source: "actual"`), so the scheduler interface is unchanged;
- the current week is rebuilt for its remaining days, later weeks from that history; pinned
  sessions from F3 are kept;
- every change carries a reason code and is listed in the diff. Nothing is pushed automatically.

**Rules** (a data table, all tunable):
- **Missed key session**: moved to the first valid day if the 48 h spacing and the long-session
  rule allow it, else dropped. Never stacked. The ladder rung advances only after a key session
  completed at 80% or more (same `Session.rung` mechanism as F3).
- **Overreach guard**: the reference load for the ramp caps is `min(actual, planned x 1.10)`, so one
  oversized week cannot raise every later cap.
- **Under-training**: a week below 70% of plan does not trigger catch-up; the next week is capped at
  the previous reference.
- **Return from break**: the F2 rule, applied to gaps detected in the history. If F6 confirms
  `SICK`, `INJURED` and `HOLIDAY` calendar events exist, they are read as `off` exceptions.
- **Readiness gating**, opt-in (`preferences.adaptation.use_wellness`): when HRV (against the athlete's
  own 28-day baseline) or TSB is below its threshold on two consecutive days, the next key
  session is downgraded one rung or turned into endurance with the existing `swap.py` mechanics.
  Never upgrades. This is a training-load heuristic, not health advice.

**Done when.**
- Scenarios: everything done, so the plan is unchanged; a missed week; an overreach week; a
  10-day gap; two low-readiness days.
- Property test: an adapted plan always passes `validate_plan` against its actual history.
- Adapting twice gives the same output (idempotent, deterministic for a given plan, history and day).

---

## M11: Broader goal types

### F9. New goal types and vertical gain

**Why.** `goal.target.elevation_m` is informative only, and a 10k, half or MTB goal is all that
exists for running and road events.

**What.**
- New `GoalType` values `run_marathon`, `trail_run` and `gran_fondo`, added with the README
  recipe: enum, `GOAL_SPORT`, a `GoalSpec` in `specificity.py`. The property test covers them
  automatically. Taper length, long-session ceiling and recommended hours are data, listed with
  their rationale in the README rules table.
- Vertical gain becomes a planned quantity for `trail_run`, `mtb_marathon`, `ultra_endurance` and
  `gran_fondo` when `elevation_m` is set: the long session carries a target ascent (`Session.ascent_m`)
  that ramps with the same caps as duration, aiming at the race's ascent per hour.
  It appears in the session note and the intervals.icu description (the text syntax has no
  elevation field).
- New warning `vertical_gain_gap` when the peak long session reaches less than 70% of the race's
  ascent per hour.

**Done when.** Each new goal type passes the property test and has a scenario test; the warning
has a tampering test; load stays based on duration and IF (no grade adjustment yet, see backlog).

---

## Backlog (not committed)

- Double days (two endurance sessions in one day).
- Template preferences: `preferences.avoid_templates` and `prefer_templates`.
- Training-camp blocks (planned overreach followed by a forced recovery).
- Offline exports: `.zwo` / `.mrc`, FIT workouts, `.ics` calendar.
- `plan diff` between two plans (for example before and after `adapt` or a new FTP).
- Load calibration report: planned vs actual TSS by sport, with a suggested `run_impact_multiplier`.
- Grade-adjusted pace and running power for trail and hilly runs.
- Maintenance block after the last goal.
- Natural-language front for amendments ("replace Thursday's run by an hour of MTB") calling the
  F3 library functions; the CLI flags are the stable interface it would use.

## Out of scope (unchanged)

GUI or web front-end, nutrition engine (long-session fueling notes stay), medical advice.
Auto-adaptation was out of scope until now: it moves into scope with F3, F7 and F8 because the
hook and the history-based scheduler are ready.

## Assumptions made in this proposal

- F2 supports `off` and `limited` only; camp blocks would break the safety caps on purpose, so
  they stay in the backlog.
- Amendments are stored in the plan, not in the profile, so the profile stays purely hand-edited.
- The default amendment scope is `rest`, because the aim is a plan that follows the change;
  `--dry-run` shows the diff first and nothing is pushed by itself.
- F5 is opt-in so that existing plans do not change.
- Numeric thresholds (85% and 70% return caps, 80% rung rule, TSB band, 70% under-training,
  10% drift) are starting values, kept in `rules.py` and calibrated on the property-test
  profiles before release.
- Wellness gating is opt-in.
- Strength is outside the ramp caps because it is not aerobic load; it constrains placement and
  the time budget, and a flat estimate (35 TSS per hour, tunable) feeds the CTL projection and
  the intervals.icu forecast only.
- Default strength equipment is bodyweight, dumbbells and a barbell; prescriptions use reps in
  reserve because no 1RM is known; no plyometrics.
