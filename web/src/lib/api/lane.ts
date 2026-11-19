import { browserErrors } from "@/i18n/browser";
import { ApiError, errorFromResponse } from "./errors";
import {
    storedGeneration,
    writeGeneration,
    type Generation,
} from "./generation";

export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type LaneRequest = { method: Method; url: string; body?: unknown };
export type Exchange = { status: number; ok: boolean; data: unknown };
export type Owner = number | null;
// What the tab's last adopted session read confirmed; a request belongs to the confirmation it was
// prepared under.
export type Confirmation = { generation: Generation; owner: Owner };

// Above the 90 s Gunicorn worker timeout, so the server answers or fails first.
const TIMEOUT_MS = 95_000;
// session_busy is refused before any view runs, so one retry is safe for every method.
const MAX_RETRY_SECONDS = 2;

let csrfToken: string | undefined;
let epoch = 0;
let serial: Promise<unknown> = Promise.resolve();
let confirmation: Confirmation | undefined;
let sessionHeld: () => Promise<Owner> = () => Promise.resolve(null);
let staleSession = () => {};
const unreadable = Symbol("unreadable");

export function setCsrfToken(token: string | undefined) {
    csrfToken = token;
}

export function mutationEpoch() {
    return epoch;
}

export function confirmSessionWith(check: () => Promise<Owner>) {
    sessionHeld = check;
}

export function onStaleSession(handler: () => void) {
    staleSession = handler;
}

export function confirmSession(value: Confirmation) {
    confirmation = value;
}

export function confirmedSession() {
    return confirmation;
}

// A tab that never confirmed a session has nothing to compare and counts as current.
export function sessionCurrent() {
    return !outdated(confirmation);
}

function outdated(prepared: Confirmation | undefined) {
    return prepared !== undefined && prepared.generation !== storedGeneration();
}

// A session read is how a tab confirms a generation, so it belongs to none.
export function preparation(request: LaneRequest) {
    return request.method === "GET" && request.url === "/api/session/"
        ? undefined
        : confirmation;
}

// The caller of a request from another generation gets an outcome no screen acts on.
function dropped<T>(): Promise<T> {
    staleSession();
    return new Promise<T>(() => {});
}

function unsafe(method: Method) {
    return method !== "GET";
}

function changesSession(url: string) {
    return url.startsWith("/api/session/") || url.startsWith("/api/auth/");
}

// Answers after which the browser holds another session: a sign-out, a sign-in completed from a
// signed-out tab, and a password change, which signs the session out.
function announces(
    { method, url }: LaneRequest,
    { data }: Exchange,
    prepared: Confirmation | undefined,
) {
    if (!url.startsWith("/api/auth/") || !unsafe(method)) return false;
    if (method === "DELETE" && url.endsWith("/auth/session")) return true;
    const meta = (
        data as {
            meta?: { is_authenticated?: boolean; password_changed?: boolean };
        } | null
    )?.meta;
    return (
        meta?.password_changed === true ||
        (meta?.is_authenticated === true && (prepared?.owner ?? null) === null)
    );
}

export function exchange(
    request: LaneRequest,
    prepared = preparation(request),
): Promise<Exchange> {
    if (outdated(prepared)) return dropped();
    if (unsafe(request.method)) epoch++;
    return queue(request).then(
        (answer) => {
            const late = outdated(prepared);
            if (announces(request, answer, prepared)) {
                writeGeneration();
                staleSession();
            }
            return late ? dropped<Exchange>() : answer;
        },
        (error: unknown) => {
            if (outdated(prepared)) return dropped<Exchange>();
            throw error;
        },
    );
}

function queue(request: LaneRequest) {
    if (!changesSession(request.url)) return attempt(request);
    const result = serial.then(() => attempt(request));
    serial = result.catch(() => {});
    return result;
}

export async function send<T>(request: LaneRequest): Promise<T> {
    const prepared = preparation(request);
    const first = await exchange(request, prepared);
    if (first.ok) return first.data as T;
    const error = errorFromResponse(first.status, first.data);
    // session_ended answers after the request's own work committed, so only a refusal is repeated.
    if (
        !(await stillSignedIn(request, error, prepared)) ||
        error.code !== "not_authenticated"
    )
        throw error;
    const retry = await exchange(request, prepared);
    if (retry.ok) return retry.data as T;
    const refused = errorFromResponse(retry.status, retry.data);
    await stillSignedIn(request, refused, prepared);
    throw refused;
}

// A request sent with a session key that a reauthentication or sign-in replaced meanwhile is
// answered as signed out; only a session read sent after that answer decides.
function stillSignedIn(
    request: LaneRequest,
    error: ApiError,
    prepared: Confirmation | undefined,
) {
    if (
        changesSession(request.url) ||
        (error.code !== "not_authenticated" && error.code !== "session_ended")
    )
        return Promise.resolve(false);
    return repeatable(prepared);
}

// Within the generation the request was prepared under, and only for the owner it was prepared
// for; a generation change on the way drops the request instead.
export async function repeatable(prepared: Confirmation | undefined) {
    const owner = await sessionHeld().catch(() => null);
    if (outdated(prepared)) return dropped<boolean>();
    return (
        owner !== null && (prepared === undefined || prepared.owner === owner)
    );
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
