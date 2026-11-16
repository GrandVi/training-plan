# endurance-planner

Multi-sport (cycling + running) training plan generator with intervals.icu export.

Status: milestone 1 of 6 (models, zones, load, profile loading). The full README
(quick start, scientific rationale, how to extend) comes with milestone 6.

## Development

    pip install -e ".[dev]"
    pytest && ruff check . && ruff format --check . && mypy

The athlete profile is documented in `examples/profile.example.yaml`.

## Assumptions

Defaults picked where the spec left a choice. All of them are data or constants
that can be tuned (`zones.py`, `thresholds.py`, `preferences.limits` in the profile).

- **Run intensity is a fraction of threshold speed**, not of pace: 105% means 5%
  faster than threshold pace (pace = threshold pace / fraction).
- **Planned TSS** of a step is `hours * IF^2 * 100` and a session is the sum of its
  steps. No normalized-power smoothing, so very variable sessions are slightly
  underestimated compared with a recorded TSS. Run load uses the same formula on
  speed (rTSS without grade adjustment).
- **HR and RPE targets** are converted to an intensity factor with piecewise-linear
  tables (`HR_TO_IF`, `RPE_TO_IF`), anchored on matching zone boundaries. The same
  tables give the HR guide shown next to power and pace targets. No HR guide is
  given above 106% of threshold (HR lags on short hard efforts).
- **Low/high intensity split** is by time in zone, per step, using the target
  midpoint: low means at or below the top of the model's low zones (Coggan Z2 = 75%
  FTP, Daniels E = 84% of threshold speed, HR Z2 = 89% LTHR, RPE 4). Recoveries
  between efforts count as low. Tempo and sweet spot count as high.
- **Zone models**: zones are contiguous, defined by inclusive upper bounds.
  `coggan_7` is the standard Coggan table. `daniels_5` is a contiguous E/M/T/I/R
  approximation relative to T pace (Daniels' own zones have gaps). `lthr_5` is
  Friel's LTHR zones with 5a/5b/5c merged, with sport-specific bounds.
- **Threshold pace from a race**: Riegel (exponent 1.06), taken as the pace of the
  distance covered in 60 minutes. Flagged as estimated.
- **FTP from a 20-min test**: 0.95 x, via `thresholds.bike.ftp_20min_w`.
- **Max HR from age**: Tanaka (208 - 0.7 x age), only when `max_hr_bpm` is absent.
- **Threshold inconsistencies are warnings**, not errors (LTHR >= max HR, W/kg
  outside 1.0-6.5, threshold pace vs recent race differing by more than 5%).
  Structural problems in the profile (missing anchors, impossible availability,
  unknown keys) are hard errors.
- **Distance-based steps** (e.g. 5 x 1 km) keep `distance_m` as the prescription;
  `duration_s` is an estimate recomputed from the threshold pace on retargeting.
- **ultra_endurance** goals use `goal.sport`, defaulting to `preferences.primary_sport`.
- A weekday missing from `availability.weekdays` is a rest day.

## Disclaimer

This tool generates generic training plans from rules of thumb. It is not medical
advice and does not replace a coach or a physician.
