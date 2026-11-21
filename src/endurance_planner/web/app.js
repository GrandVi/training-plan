// endurance-planner web app: week view, session detail, changes with a preview first,
// unavailable dates, push to intervals.icu, amendment log. No framework, no build step.
import { ApiError, OFFLINE_MESSAGE, clearToken, get, getToken, post, setToken } from "./api.js";
import {
  ACTIVITY,
  SPORT,
  addDays,
  daysBetween,
  formatDay,
  formatDuration,
  formatShort,
  h,
  icon,
  today,
  words,
} from "./dom.js";

const view = document.getElementById("view");
const status = document.getElementById("status");
const offlineBanner = document.getElementById("offline");
const revisionLabel = document.getElementById("revision");

const state = { overview: null, freshStatus: false };
// Changes shown together under one line: they follow from the change, they are not it.
const BACKGROUND = new Set(["rebalanced", "replanned", "regenerated"]);
const STEP_KIND = { warmup: "Warm-up", cooldown: "Cool-down", recovery: "Recovery", work: "" };

const CHANGE_LABELS = {
  amended: "Changed",
  skipped: "Skipped",
  moved: "Moved away",
  yielded_to_amendment: "Adjusted (same week)",
  rung_held: "Repeats its level",
  rebalanced: "Later week rebalanced",
  calendar_exception: "Calendar change",
  undone: "Undone",
  replanned: "Rebuilt",
  regenerated: "New plan",
};

// ---------------------------------------------------------------------- helpers
function say(text) {
  status.replaceChildren(text ? h("p", { text }) : "");
  state.freshStatus = Boolean(text);
}

function setOffline(offline) {
  offlineBanner.hidden = !offline;
}

function render(title, tab, ...children) {
  document.title = `${title} · Training plan`;
  for (const link of document.querySelectorAll(".tabs a")) {
    if (link.dataset.tab === tab) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const heading = h("h1", { text: title, attrs: { tabindex: "-1" } });
  view.replaceChildren(heading, ...children.flat().filter(Boolean));
  heading.focus();
}

function revision() {
  return state.overview ? state.overview.revision : 0;
}

async function overview(force = false) {
  if (!state.overview || force) {
    const { data, cached } = await get("/plan");
    state.overview = data;
    setOffline(cached);
    revisionLabel.textContent = `revision ${data.revision}`;
  }
  return state.overview;
}

function currentWeek(plan) {
  if (plan.current_week) return plan.current_week;
  return today() < plan.start ? 1 : plan.weeks.length;
}

function weekOf(plan, date) {
  return Math.floor(daysBetween(plan.start, date) / 7) + 1;
}

function sessionLabel(session) {
  if (!session) return "rest";
  return `${SPORT[session.sport]} ${formatDuration(session.duration_s)} · ${session.name}`;
}

function field(label, input, hint) {
  const id = input.id || `f-${Math.random().toString(36).slice(2, 9)}`;
  input.id = id;
  return h(
    "div",
    { class: "field" },
    h("label", { attrs: { for: id } }, label, hint ? h("span", { class: "hint", text: ` (${hint})` }) : null),
    input,
  );
}

function select(name, options, value = "") {
  return h(
    "select",
    { name },
    options.map(([v, label]) => h("option", { value: v, text: label, selected: v === value })),
  );
}

function radios(legend, name, options, value) {
  return h(
    "fieldset",
    {},
    h("legend", { text: legend }),
    options.map(([v, label]) =>
      h("label", { class: "choice" }, h("input", { type: "radio", name, value: v, checked: v === value }), label),
    ),
  );
}

function errorBox(error) {
  if (error instanceof ApiError && error.status === 409) {
    return h(
      "div",
      { class: "alert", attrs: { role: "alert" } },
      h("p", { text: "The plan changed elsewhere (on another device or from the command line)." }),
      h("p", { text: "Reload it to see the latest version, then make your change again." }),
      h("button", {
        type: "button",
        text: "Reload the plan",
        on: { click: async () => { await overview(true); route(); } },
      }),
    );
  }
  if (error instanceof ApiError && error.status === 401) {
    showToken("The planner refused the token. Enter it again.");
    return h("p", { class: "alert", text: "Token refused." });
  }
  const issues = (error.details && error.details.issues) || [];
  return h(
    "div",
    { class: "alert", attrs: { role: "alert" } },
    h("p", { text: error.message || String(error) }),
    issues.length ? renderIssues(issues) : null,
  );
}

function changeList(changes) {
  return h(
    "ul",
    { class: "changes" },
    changes.map((change) =>
      h(
        "li",
        {},
        h("span", { class: `code ${change.code}`, text: `${formatDay(change.date)} · ${CHANGE_LABELS[change.code] || words(change.code)}` }),
        `${sessionLabel(change.before)} → ${sessionLabel(change.after)}`,
      ),
    ),
  );
}

function renderChanges(changes) {
  if (!changes.length) return h("p", { class: "muted", text: "No session changes." });
  const main = changes.filter((change) => !BACKGROUND.has(change.code));
  const background = changes.filter((change) => BACKGROUND.has(change.code));
  return [
    main.length ? changeList(main) : null,
    background.length
      ? h(
          "details",
          { class: "more" },
          h("summary", { text: `${background.length} later session(s) adjusted to follow (ramp limits, levels)` }),
          changeList(background),
        )
      : null,
  ];
}

function renderIssues(issues) {
  if (!issues.length) return null;
  return h(
    "ul",
    { class: "issues" },
    issues.map((issue) =>
      h("li", { class: issue.severity }, h("strong", { text: issue.severity === "error" ? "Rule broken: " : "Note: " }), issue.message),
    ),
  );
}

/** Preview (dry run) of a change in `slot`, then a confirm button that applies it. */
async function preview(slot, endpoint, payload, done) {
  slot.replaceChildren(h("p", { class: "muted", text: "Preparing the preview…" }));
  let result;
  try {
    ({ data: result } = await post(endpoint, { ...payload, expected_revision: revision(), dry_run: true }));
  } catch (error) {
    slot.replaceChildren(errorBox(error));
    return;
  }
  const force = h("input", { type: "checkbox", id: "force" });
  const confirm = h("button", { type: "button", text: result.refused ? "Apply anyway" : "Confirm" });
  confirm.disabled = result.refused;
  force.addEventListener("change", () => { confirm.disabled = !force.checked; });
  const feedback = h("div", {});
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    feedback.replaceChildren(h("p", { class: "muted", text: "Saving…" }));
    try {
      const body = { ...payload, expected_revision: revision(), dry_run: false, force: result.refused && force.checked };
      const { data } = await post(endpoint, body);
      state.overview = null;
      say(`Saved${data.message ? `: ${data.message}` : ""} (revision ${data.revision}).`);
      done(data);
    } catch (error) {
      feedback.replaceChildren(errorBox(error));
      confirm.disabled = false;
    }
  });
  const cancel = h("button", { type: "button", class: "secondary", text: "Cancel", on: { click: () => slot.replaceChildren() } });
  const title = h("h2", { text: "Preview", attrs: { tabindex: "-1" } });
  slot.replaceChildren(
    h(
      "section",
      { class: "preview", attrs: { "aria-label": "Preview of the change" } },
      title,
      result.message ? h("p", { text: result.message }) : null,
      renderChanges(result.changes),
      renderIssues(result.issues),
      result.refused
        ? h("label", { class: "choice" }, force, "This change breaks a rule above: apply it anyway")
        : null,
      h("div", { class: "actions" }, confirm, cancel),
      feedback,
    ),
  );
  title.focus();
}

// ---------------------------------------------------------------------- screens
function showToken(message) {
  const input = h("input", { type: "password", name: "token", required: true, autocomplete: "off", spellcheck: false });
  const form = h(
    "form",
    {
      on: {
        submit: (event) => {
          event.preventDefault();
          setToken(input.value.trim());
          state.overview = null;
          say("Token saved on this device.");
          route();
        },
      },
    },
    field("Access token", input, "PLANNER_TOKEN of the server"),
    h("div", { class: "actions" }, h("button", { type: "submit", text: "Save" })),
  );
  render(
    "Connect to the planner",
    "settings",
    message ? h("p", { class: "alert", text: message }) : null,
    h("p", { text: "Enter the access token once; it stays on this device." }),
    form,
  );
}

function showNoPlan() {
  const slot = h("div", {});
  render(
    "No plan yet",
    "week",
    h("p", { text: "The server has a profile but no plan. Generate one from the profile." }),
    h("div", { class: "actions" }, h("button", { type: "button", text: "Preview a new plan", on: { click: () => preview(slot, "/plan/generate", {}, () => go("#/")) } })),
    slot,
  );
}

async function showWeek(index) {
  const plan = await overview();
  const number = Math.min(Math.max(index || currentWeek(plan), 1), plan.weeks.length);
  const { data: week, cached } = await get(`/plan/weeks/${number}`);
  setOffline(cached);
  const phase = week.recovery ? `${week.phase}, recovery` : week.phase;
  const nav = h(
    "nav",
    { class: "week-nav", attrs: { "aria-label": "Weeks" } },
    h("button", { type: "button", class: "secondary", text: "← Previous", disabled: number <= 1, on: { click: () => go(`#/week/${number - 1}`) } }),
    h("button", { type: "button", class: "secondary", text: "This week", on: { click: () => go(`#/week/${currentWeek(plan)}`) } }),
    h("button", { type: "button", class: "secondary", text: "Next →", disabled: number >= plan.weeks.length, on: { click: () => go(`#/week/${number + 1}`) } }),
  );
  const cards = week.sessions.map((session) => {
    const badges = [];
    if (session.hard) badges.push(h("span", { class: "badge hard", text: "key" }));
    if (session.pinned) badges.push(h("span", { class: "badge pinned", text: "changed" }));
    if (session.indoor) badges.push(h("span", { class: "badge", text: "indoor" }));
    if (session.activity_type === "MountainBikeRide") badges.push(h("span", { class: "badge", text: "MTB" }));
    return h(
      "li",
      {},
      h(
        "a",
        { class: "card", href: `#/session/${session.date}` },
        h(
          "span",
          { class: "day" },
          h("span", { text: formatDay(session.date).split(" ")[0] }),
          h("span", { class: "date", text: formatShort(session.date) }),
        ),
        icon(session.sport),
        h("span", { class: "name" }, h("span", { class: "sr-only", text: `${SPORT[session.sport]}: ` }), session.name, badges),
        h("span", { class: "meta", text: `${formatDuration(session.duration_s)} · ${Math.round(session.tss)} TSS · ${words(session.intent)}` }),
      ),
    );
  });
  render(
    `Week ${week.index} of ${week.weeks} · ${phase}`,
    "week",
    h(
      "p",
      { class: "summary" },
      h("span", { text: `${formatShort(week.start)} – ${formatShort(week.end)}` }),
      h("span", { text: `${formatDuration(week.duration_s)}` }),
      h("span", { text: `${Math.round(week.tss)} TSS` }),
      h("span", { text: `load ${Math.round(week.load)}` }),
      h("span", { text: `${week.low_pct}% easy` }),
    ),
    week.races.length ? h("p", { class: "note", text: `Race this week: ${week.races.join(", ")}` }) : null,
    week.exceptions.map((text) => h("p", { class: "note", text: `Calendar: ${text}` })),
    nav,
    cards.length ? h("ul", { class: "cards" }, cards) : h("p", { class: "muted", text: "No session this week." }),
  );
}

async function findSession(date) {
  const plan = await overview();
  const number = weekOf(plan, date);
  if (number < 1 || number > plan.weeks.length) return [null, number];
  const { data: week, cached } = await get(`/plan/weeks/${number}`);
  setOffline(cached);
  return [week.sessions.find((s) => s.date === date) || null, number, week];
}

async function showSession(date) {
  const [session, number] = await findSession(date);
  if (!session) {
    render("No session", "week", h("p", { text: `There is no session on ${formatDay(date)}.` }), h("a", { href: `#/week/${number}`, text: "Back to the week" }));
    return;
  }
  const steps = session.steps.map((line) => {
    if (line.repeat) return h("li", { class: `depth-${line.depth} repeat`, text: `${line.repeat} ×` });
    const size = line.distance_m ? `${line.distance_m >= 1000 ? `${line.distance_m / 1000} km` : `${line.distance_m} m`} (~${formatDuration(line.duration_s)})` : formatDuration(line.duration_s);
    const kind = STEP_KIND[line.kind] ? `${STEP_KIND[line.kind]} ` : "";
    return h(
      "li",
      { class: `depth-${line.depth}` },
      `${kind}${size} · ${line.target}`,
      line.note ? ` · ${line.note}` : "",
      line.absolute ? h("span", { class: "abs", text: line.absolute }) : null,
    );
  });
  render(
    session.name,
    "week",
    h(
      "p",
      { class: "summary" },
      h("span", { text: formatDay(session.date) }),
      h("span", { text: ACTIVITY[session.activity_type] || session.activity_type }),
      h("span", { text: formatDuration(session.duration_s) }),
      h("span", { text: `${Math.round(session.tss)} TSS` }),
      h("span", { text: words(session.intent) }),
    ),
    session.pinned ? h("p", { class: "note", text: `Changed by amendment ${session.amendment_id}: replans keep it as it is.` }) : null,
    h("h2", { text: "Steps" }),
    h("ul", { class: "steps" }, steps),
    h("h2", { text: "Why" }),
    h("p", { text: session.rationale }),
    session.notes ? h("p", { class: "muted", text: session.notes }) : null,
    h(
      "div",
      { class: "actions" },
      h("a", { class: "button", href: `#/change/${session.date}`, text: "Change this session" }),
      h("a", { class: "button secondary", href: `#/week/${number}`, text: "Back to the week" }),
    ),
  );
}

async function showChange(date) {
  const [session, number, week] = await findSession(date);
  if (!session) {
    render("No session", "week", h("p", { text: `There is no session on ${formatDay(date)}.` }));
    return;
  }
  const minutes = h("input", { type: "number", name: "minutes", min: 1, max: 600, inputMode: "numeric", placeholder: String(Math.round(session.duration_s / 60)) });
  const sport = select("sport", [["", `Keep (${SPORT[session.sport]})`], ["bike", "Ride"], ["run", "Run"]]);
  const activity = select("activity", [["", "Keep"], ["road", "Road"], ["mtb", "MTB"], ["trainer", "Trainer (indoor)"]]);
  const intent = select("intent", [["", `Keep (${words(session.intent)})`], ["endurance", "Easier: endurance"], ["recovery", "Easier: recovery"]]);
  const moveTo = h("input", { type: "date", name: "move_to", min: week.start, max: week.end });
  const skip = h("input", { type: "checkbox", name: "skip" });
  const reason = h("input", { type: "text", name: "reason", maxLength: 200, autocomplete: "off" });
  const others = [minutes, sport, activity, intent, moveTo];
  skip.addEventListener("change", () => { for (const input of others) input.disabled = skip.checked; });
  const slot = h("div", {});
  const form = h(
    "form",
    {
      on: {
        input: () => slot.replaceChildren(), // a new preview is needed after an edit
        submit: (event) => {
          event.preventDefault();
          const data = new FormData(form);
          const payload = { date, scope: data.get("scope"), reason: reason.value.trim() };
          if (skip.checked) payload.skip = true;
          else {
            if (minutes.value) payload.minutes = Number(minutes.value);
            if (sport.value) payload.sport = sport.value;
            if (activity.value) payload.activity = activity.value;
            if (intent.value) payload.intent = intent.value;
            if (moveTo.value) payload.move_to = moveTo.value;
          }
          preview(slot, "/plan/amend", payload, () => go(`#/week/${number}`));
        },
      },
    },
    field("New duration in minutes", minutes, "empty: keep it"),
    field("Sport", sport),
    field("Bike type", activity, "for rides"),
    field("Intensity", intent),
    field("Move to", moveTo, "a day of the same week"),
    h("label", { class: "choice" }, skip, "Skip this session"),
    field("Reason", reason, "optional, kept in the log"),
    radios("Adapt", "scope", [["session", "Only this session"], ["week", "This week"], ["rest", "This week and the rest of the plan"]], "rest"),
    h("div", { class: "actions" }, h("button", { type: "submit", text: "Preview the change" })),
  );
  render(`Change: ${session.name}`, "week", h("p", { class: "muted", text: `${formatDay(session.date)} · ${sessionLabel(session)}` }), form, slot);
}

function showUnavailable() {
  const start = h("input", { type: "date", name: "from", required: true, min: today(), value: today() });
  const end = h("input", { type: "date", name: "to", min: today() });
  const run = h("input", { type: "number", name: "run", min: 0, max: 600, inputMode: "numeric" });
  const bike = h("input", { type: "number", name: "bike", min: 0, max: 600, inputMode: "numeric" });
  const activity = select("activity", [["", "Any"], ["road", "Road"], ["mtb", "MTB"], ["trainer", "Trainer (indoor)"]]);
  const reason = h("input", { type: "text", name: "reason", maxLength: 200, autocomplete: "off" });
  const limits = h("fieldset", {}, h("legend", { text: "Minutes per day (empty: as usual, 0: not that sport)" }), h("div", { class: "row" }, field("Run", run), field("Ride", bike)), field("Bike type", activity));
  const slot = h("div", {});
  const form = h(
    "form",
    {
      on: {
        input: () => slot.replaceChildren(),
        change: () => { limits.disabled = new FormData(form).get("mode") !== "limited"; },
        submit: (event) => {
          event.preventDefault();
          const data = new FormData(form);
          const payload = { from: start.value, scope: data.get("scope"), reason: reason.value.trim() };
          if (end.value) payload.to = end.value;
          if (data.get("mode") === "limited") {
            const minutes = {};
            if (run.value !== "") minutes.run = Number(run.value);
            if (bike.value !== "") minutes.bike = Number(bike.value);
            if (Object.keys(minutes).length) payload.minutes = minutes;
            if (activity.value) payload.activity = activity.value;
          }
          preview(slot, "/plan/unavailable", payload, () => go("#/"));
        },
      },
    },
    h("div", { class: "row" }, field("From", start), field("To", end, "empty: one day")),
    radios("These days", "mode", [["off", "No training"], ["limited", "Limited time"]], "off"),
    limits,
    field("Reason", reason, "optional, e.g. business trip"),
    radios("Adapt", "scope", [["week", "Only the weeks of these days"], ["rest", "These weeks and the rest of the plan"]], "rest"),
    h("div", { class: "actions" }, h("button", { type: "submit", text: "Preview the change" })),
  );
  limits.disabled = true;
  render("Unavailable days", "unavailable", form, slot);
}

function showPush() {
  const start = h("input", { type: "date", name: "from", value: today() });
  const end = h("input", { type: "date", name: "to" });
  const absolute = h("input", { type: "checkbox", name: "absolute_targets" });
  const slot = h("div", {});
  const payload = () => {
    const body = { absolute_targets: absolute.checked };
    if (start.value) body.from = start.value;
    if (end.value) body.to = end.value;
    return body;
  };
  const form = h(
    "form",
    {
      on: {
        input: () => slot.replaceChildren(),
        submit: async (event) => {
          event.preventDefault();
          slot.replaceChildren(h("p", { class: "muted", text: "Preparing the preview…" }));
          try {
            const { data } = await post("/plan/push", { ...payload(), dry_run: true });
            const send = h("button", { type: "button", text: `Send ${data.events} workout(s) to intervals.icu` });
            const result = h("div", {});
            send.addEventListener("click", async () => {
              send.disabled = true;
              result.replaceChildren(h("p", { class: "muted", text: "Sending…" }));
              try {
                const { data: done } = await post("/plan/push", { ...payload(), dry_run: false });
                result.replaceChildren(
                  h("p", { text: `Sent ${done.upserted} workout(s), removed ${done.pruned} stale event(s), ${done.unchanged} unchanged.` }),
                  done.threshold_diffs.length
                    ? renderIssues(done.threshold_diffs.map((message) => ({ severity: "warning", message: `Thresholds differ: ${message}` })))
                    : null,
                );
                say("Pushed to intervals.icu.");
              } catch (error) {
                result.replaceChildren(errorBox(error));
                send.disabled = false;
              }
            });
            const title = h("h2", { text: "Preview", attrs: { tabindex: "-1" } });
            slot.replaceChildren(
              h(
                "section",
                { class: "preview", attrs: { "aria-label": "Preview of the push" } },
                title,
                h("p", { text: `${data.events} workout(s) from ${formatDay(data.start)}${data.end ? ` to ${formatDay(data.end)}` : ""}. Only the ones that changed are written.` }),
                h("div", { class: "actions" }, send),
                h(
                  "details",
                  { class: "more" },
                  h("summary", { text: `Show the ${data.events} workout(s)` }),
                  h("ul", { class: "changes" }, data.payloads.map((event) => h("li", {}, h("span", { class: "code", text: formatDay(event.start_date_local.slice(0, 10)) }), `${event.type} · ${event.name}`))),
                ),
                result,
              ),
            );
            title.focus();
          } catch (error) {
            slot.replaceChildren(errorBox(error));
          }
        },
      },
    },
    h("div", { class: "row" }, field("From", start), field("To", end, "empty: to the end")),
    h("label", { class: "choice" }, absolute, "Watts and paces instead of % of threshold"),
    h("div", { class: "actions" }, h("button", { type: "submit", text: "Preview the push" })),
  );
  render("Push to intervals.icu", "push", h("p", { class: "muted", text: "Nothing is sent before you confirm." }), form, slot);
}

async function showAmendments() {
  await overview();
  const { data: amendments, cached } = await get("/plan/amendments");
  setOffline(cached);
  const items = amendments.map((amendment) => {
    const slot = h("div", {});
    const undo = amendment.active
      ? h("button", { type: "button", class: "secondary", text: `Undo ${amendment.id}`, on: { click: () => preview(slot, `/plan/amendments/${amendment.id}/undo`, {}, () => go("#/amendments")) } })
      : null;
    return h("li", { class: amendment.active ? "" : "inactive" }, h("p", { text: amendment.line }), undo, slot);
  });
  render(
    "Changes",
    "amendments",
    items.length ? h("ul", { class: "amendments" }, items) : h("p", { class: "muted", text: "No change yet: open a session to change it." }),
  );
}

async function showSettings() {
  const { data: profile } = await get("/profile/summary");
  const replanSlot = h("div", {});
  const generateSlot = h("div", {});
  render(
    "Settings",
    "settings",
    h("h2", { text: profile.athlete }),
    h("dl", { class: "thresholds" }, profile.thresholds.map((t) => [h("dt", { text: t.name }), h("dd", { text: `${t.value}${t.estimated ? " (estimated)" : ""}` })])),
    renderIssues(profile.warnings),
    h("p", { class: "muted", text: "The profile is edited on the server (profile.yaml)." }),
    h("h2", { text: "Plan" }),
    h("p", { text: "After editing profile.yaml, rebuild the plan from today: past and changed sessions stay." }),
    h("div", { class: "actions" }, h("button", { type: "button", class: "secondary", text: "Preview a replan", on: { click: () => preview(replanSlot, "/plan/replan", {}, () => go("#/")) } })),
    replanSlot,
    h("p", { text: "A new plan replaces the current one and its changes." }),
    h("div", { class: "actions" }, h("button", { type: "button", class: "secondary", text: "Preview a new plan", on: { click: () => preview(generateSlot, "/plan/generate", {}, () => go("#/")) } })),
    generateSlot,
    h("h2", { text: "This device" }),
    h("div", { class: "actions" }, h("button", { type: "button", class: "danger", text: "Forget the token", on: { click: () => { clearToken(); showToken("Token removed from this device."); } } })),
  );
}

// ---------------------------------------------------------------------- router
const ROUTES = [
  [/^#?\/?$/, () => showWeek()],
  [/^#\/week\/(\d+)$/, (m) => showWeek(Number(m[1]))],
  [/^#\/session\/(\d{4}-\d{2}-\d{2})$/, (m) => showSession(m[1])],
  [/^#\/change\/(\d{4}-\d{2}-\d{2})$/, (m) => showChange(m[1])],
  [/^#\/unavailable$/, () => showUnavailable()],
  [/^#\/push$/, () => showPush()],
  [/^#\/amendments$/, () => showAmendments()],
  [/^#\/settings$/, () => showSettings()],
];

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function route() {
  if (!state.freshStatus) say("");
  state.freshStatus = false;
  if (!getToken()) {
    showToken();
    return;
  }
  const hash = location.hash || "#/";
  const match = ROUTES.map(([pattern, screen]) => [hash.match(pattern), screen]).find(([m]) => m);
  try {
    if (match) await match[1](match[0]);
    else await showWeek();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) showToken("The planner refused the token. Enter it again.");
    else if (error instanceof ApiError && error.code === "no_plan") showNoPlan();
    else render("Something went wrong", "", errorBox(error), error.code === "offline" ? h("p", { text: OFFLINE_MESSAGE }) : null);
  }
}

window.addEventListener("hashchange", route);
window.addEventListener("offline", () => setOffline(true));
window.addEventListener("online", () => { setOffline(false); state.overview = null; });
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {
    // Not available (plain http on another host): the app works without offline reading.
  });
}
route();
