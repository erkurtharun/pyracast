import { browserErrors } from "@/i18n/browser";
import { ApiError, errorFromResponse } from "./errors";

export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type LaneRequest = { method: Method; url: string; body?: unknown };
export type Exchange = { status: number; ok: boolean; data: unknown };

// Above the 90 s Gunicorn worker timeout, so the server answers or fails first.
const TIMEOUT_MS = 95_000;
// session_busy is refused before any view runs, so one retry is safe for every method.
const MAX_RETRY_SECONDS = 2;

let csrfToken: string | undefined;
let epoch = 0;
let serial: Promise<unknown> = Promise.resolve();
let sessionHeld: () => Promise<boolean> = () => Promise.resolve(false);
const unreadable = Symbol("unreadable");

export function setCsrfToken(token: string | undefined) {
    csrfToken = token;
}

export function mutationEpoch() {
    return epoch;
}

export function confirmSessionWith(check: () => Promise<boolean>) {
    sessionHeld = check;
}

function unsafe(method: Method) {
    return method !== "GET";
}

function changesSession(url: string) {
    return url.startsWith("/api/session/") || url.startsWith("/api/auth/");
}

export function exchange(request: LaneRequest): Promise<Exchange> {
    if (unsafe(request.method)) epoch++;
    if (!changesSession(request.url)) return attempt(request);
    const result = serial.then(() => attempt(request));
    serial = result.catch(() => {});
    return result;
}

export async function send<T>(request: LaneRequest): Promise<T> {
    const first = await exchange(request);
    if (first.ok) return first.data as T;
    const error = errorFromResponse(first.status, first.data);
    // session_ended answers after the request's own work committed, so only a refusal is repeated.
    if (
        !(await stillSignedIn(request, error)) ||
        error.code !== "not_authenticated"
    )
        throw error;
    const retry = await exchange(request);
    if (retry.ok) return retry.data as T;
    const refused = errorFromResponse(retry.status, retry.data);
    await stillSignedIn(request, refused);
    throw refused;
}

// A request sent with a session key that a reauthentication or sign-in replaced meanwhile is
// answered as signed out; only a session read sent after that answer decides.
async function stillSignedIn(request: LaneRequest, error: ApiError) {
    if (
        changesSession(request.url) ||
        (error.code !== "not_authenticated" && error.code !== "session_ended")
    )
        return false;
    return sessionHeld().catch(() => false);
}

async function attempt(request: LaneRequest, retried = false) {
    const result = await deliver(request);
    if (retried || result.ok) return result;
    const error = errorFromResponse(result.status, result.data);
    if (error.code !== "session_busy") return result;
    const seconds = Math.min(error.retryAfter ?? 1, MAX_RETRY_SECONDS);
    await new Promise((resolve) => setTimeout(resolve, seconds * 1000));
    return attempt(request, true);
}

async function deliver({ method, url, body }: LaneRequest): Promise<Exchange> {
    const t = browserErrors();
    let response: Response;
    try {
        response = await fetch(url, {
            method,
            credentials: "same-origin",
            cache: "no-store",
            signal: AbortSignal.timeout(TIMEOUT_MS),
            headers: {
                ...(body === undefined
                    ? {}
                    : { "Content-Type": "application/json" }),
                ...(csrfToken &&
                (unsafe(method) || url.startsWith("/api/auth/"))
                    ? { "X-CSRFToken": csrfToken }
                    : {}),
            },
            ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
    } catch {
        throw new ApiError(unsafe(method) ? t("unconfirmed") : t("network"));
    }
    const { status, ok } = response;
    if (status === 204) return { status, ok, data: null };
    const data: unknown = await response.json().catch(() => unreadable);
    if (data !== unreadable) return { status, ok, data };
    if (ok) throw new ApiError(t("invalidResponse"), { status });
    return { status, ok, data: null };
}
