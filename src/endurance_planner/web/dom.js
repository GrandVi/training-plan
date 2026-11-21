// DOM helpers. Text always goes through textContent (never innerHTML), so plan data
// cannot inject markup.

/** h("p", {class: "x", on: {click: fn}, attrs: {"aria-label": "y"}}, "text", child) */
export function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "on") for (const [event, fn] of Object.entries(value)) node.addEventListener(event, fn);
    else if (key === "attrs") for (const [name, v] of Object.entries(value)) {
      if (v !== undefined && v !== null && v !== false) node.setAttribute(name, v === true ? "" : String(v));
    }
    else node[key] = value;
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const SVG = "http://www.w3.org/2000/svg";

/** Icon from the sprite in index.html; decorative, so hidden from assistive tech. */
export function icon(name) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const use = document.createElementNS(SVG, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

// Dates are plain calendar days ("2026-12-08"): handled in UTC so no time zone shifts them.
const DAY_MS = 86400000;
export const parseDay = (iso) => new Date(`${iso}T00:00:00Z`);
export const isoDay = (date) => date.toISOString().slice(0, 10);
export const addDays = (iso, days) => isoDay(new Date(parseDay(iso).getTime() + days * DAY_MS));
export const daysBetween = (a, b) => Math.round((parseDay(b) - parseDay(a)) / DAY_MS);

/** Today on this device, as YYYY-MM-DD in local time. */
export function today() {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${String(now.getDate()).padStart(2, "0")}`;
}

const dayFormat = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
const shortFormat = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
export const formatDay = (iso) => dayFormat.format(parseDay(iso));
export const formatShort = (iso) => shortFormat.format(parseDay(iso));

/** 3600 -> "1h00", 2700 -> "45 min", 45 -> "45 s" */
export function formatDuration(seconds) {
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, "0")}`;
}

export const SPORT = { bike: "Ride", run: "Run" };
export const ACTIVITY = { Ride: "Road ride", MountainBikeRide: "MTB ride", Run: "Run" };
export const words = (value) => String(value).replaceAll("_", " ");
