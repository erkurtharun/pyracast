import { browserErrors } from "@/i18n/browser";
import {
    ApiError,
    errorFromResponse,
    isContractBody,
    UnconfirmedError,
} from "./errors";

export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type LaneRequest = { method: Method; url: string; body?: unknown };
export type Exchange = { status: number; ok: boolean; data: unknown };

// Above the 90 s Gunicorn worker timeout, so the server answers or fails first.
const TIMEOUT_MS = 95_000;
// session_busy is refused before any view runs, so one retry is safe for every method.
const MAX_RETRY_SECONDS = 2;
const SESSION_READ = "/api/session/";
// Traefik's bad gateway and header timeout and Cloudflare's 520-524 do not say whether a change was applied.
const UPSTREAM_LOST = new Set([502, 504, 520, 521, 522, 523, 524]);
// The gateway also answers with this refusal for a 502 after the upstream ended, when a change may be committed.
const MAINTENANCE = "maintenance";

let csrfToken: string | undefined;
let epoch = 0;
let serial: Promise<unknown> = Promise.resolve();
// The tag of the session this tab adopted: "" while signed out, undefined before its first read.
export type Tag = string | undefined;
type SessionCheck = {
    tag: () => Tag;
    shows: (tag: string) => boolean;
    refusal: (request: LaneRequest, tag: string) => ApiError | undefined;
    confirm: () => Promise<Tag>;
};
let session: SessionCheck = {
    tag: () => undefined,
    shows: () => true,
    refusal: () => undefined,
    confirm: () => Promise.resolve(undefined),
};
const unreadable = Symbol("unreadable");

export function setCsrfToken(token: string | undefined) {
    csrfToken = token;
}

export function mutationEpoch() {
    return epoch;
}

export function confirmSessionWith(check: SessionCheck) {
    session = check;
}

export function sessionTag() {
    return session.tag();
}

function unsafe(method: Method) {
    return method !== "GET";
}

function changesSession(url: string) {
    return url.startsWith("/api/session/") || url.startsWith("/api/auth/");
}

export function changedSession() {
    return new ApiError(browserErrors()("sessionChanged"), {
        status: 409,
        code: "session_changed",
    });
}

function refusedAsChanged({ status, data }: Exchange) {
    return (
        status === 409 &&
        (data as { code?: unknown } | null)?.code === "session_changed"
    );
}

function exchange(request: LaneRequest, tag: Tag): Promise<Exchange> {
    if (unsafe(request.method)) epoch++;
    if (!changesSession(request.url)) return attempt(request, tag);
    const result = serial.then(() => attempt(request, tag));
    serial = result.catch(() => {});
    return result;
}

// The server refuses a stale tag before any view runs, so one repeat applies nothing twice.
export async function prepared(request: LaneRequest): Promise<Exchange> {
    if (request.url === SESSION_READ) return exchange(request, undefined);
    const tag = sessionTag();
    refuse(request, tag);
    let answer = await exchange(request, tag);
    if (refusedAsChanged(answer) && (await confirmed()) === tag)
        answer = await exchange(request, tag);
    if (tag !== undefined && !session.shows(tag)) throw changedSession();
    return answer;
}

function refuse(request: LaneRequest, tag: Tag) {
    const refused =
        tag === undefined ? undefined : session.refusal(request, tag);
    if (refused) throw refused;
}

async function confirmed(): Promise<Tag> {
    return session.confirm().catch(() => undefined);
}

export async function send<T>(request: LaneRequest): Promise<T> {
    const answer = await prepared(request);
    if (answer.ok) return answer.data as T;
    const error = errorFromResponse(answer.status, answer.data);
    // A session that expired or ended is learnt by a session read; the request is not repeated.
    if (
        !changesSession(request.url) &&
        (error.code === "not_authenticated" || error.code === "session_ended")
    )
        await confirmed();
    throw error;
}

async function attempt(request: LaneRequest, tag: Tag, retried = false) {
    const result = await deliver(request, tag);
    if (retried || result.ok) return result;
    const error = errorFromResponse(result.status, result.data);
    if (error.code !== "session_busy") return result;
    const seconds = Math.min(error.retryAfter ?? 1, MAX_RETRY_SECONDS);
    await new Promise((resolve) => setTimeout(resolve, seconds * 1000));
    return attempt(request, tag, true);
}

function upstreamLost(status: number, data: unknown) {
    return isContractBody(data)
        ? data.code === MAINTENANCE
        : UPSTREAM_LOST.has(status);
}

async function deliver(request: LaneRequest, tag: Tag): Promise<Exchange> {
    refuse(request, tag);
    const { method, url, body } = request;
    const t = browserErrors();
    const init: RequestInit = {
        method,
        credentials: "same-origin",
        cache: "no-store",
        // The API never redirects a request it understood, so a redirect answers nothing that was asked.
        redirect: "error",
        signal: AbortSignal.timeout(TIMEOUT_MS),
        headers: {
            ...(body === undefined
                ? {}
                : { "Content-Type": "application/json" }),
            ...(csrfToken && (unsafe(method) || url.startsWith("/api/auth/"))
                ? { "X-CSRFToken": csrfToken }
                : {}),
            ...(tag === undefined ? {} : { "X-Session-Tag": tag }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    };
    let response: Response;
    try {
        response = await fetch(url, init);
    } catch {
        if (unsafe(method)) throw new UnconfirmedError(t("unconfirmed"));
        throw new ApiError(t("network"));
    }
    const { status, ok } = response;
    if (status === 204) return { status, ok, data: null };
    const data: unknown = await response.json().catch(() => unreadable);
    if (unsafe(method) && !ok && upstreamLost(status, data))
        throw new UnconfirmedError(t("unconfirmed"), { status });
    if (data !== unreadable) return { status, ok, data };
    if (!ok) return { status, ok, data: null };
    if (unsafe(method))
        throw new UnconfirmedError(t("unconfirmed"), { status });
    throw new ApiError(t("invalidResponse"), { status });
}
