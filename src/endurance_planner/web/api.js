// Client of the planner API: bearer token, JSON, errors as ApiError.

const TOKEN_KEY = "endurance-planner.token";
let memoryToken = null; // used when localStorage is not available

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || memoryToken;
  } catch {
    return memoryToken;
  }
}

export function setToken(token) {
  memoryToken = token;
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Private mode or storage disabled: the token lasts until the page is closed.
  }
}

export function clearToken() {
  memoryToken = null;
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    // nothing stored
  }
}

export class ApiError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export const OFFLINE_MESSAGE = "You are offline: changes need a connection to the planner.";

/** Send a request; returns {data, cached} (cached: served by the service worker offline). */
export async function request(method, path, body) {
  const mutation = method !== "GET";
  if (mutation && navigator.onLine === false) throw new ApiError(0, "offline", OFFLINE_MESSAGE);
  const headers = { Accept: "application/json", Authorization: `Bearer ${getToken() || ""}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "offline", mutation ? OFFLINE_MESSAGE : "The planner cannot be reached.");
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const error = (data && data.error) || {};
    throw new ApiError(response.status, error.code || `http_${response.status}`, error.message || response.statusText, error);
  }
  return { data, cached: response.headers.get("X-Served-From") === "cache" };
}

export const get = (path) => request("GET", path);
export const post = (path, body) => request("POST", path, body);
