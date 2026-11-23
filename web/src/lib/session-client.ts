import { useEffect, useState, useSyncExternalStore } from "react";
import type { components } from "./schema";
import {
    changedSession,
    confirmSessionWith,
    mutationEpoch,
    send,
    setCsrfToken,
    type Tag,
} from "./api/lane";
import { authRequest } from "./auth-api";

export type Session = components["schemas"]["SessionOutput"];
export type SessionUser = components["schemas"]["SessionUserOutput"];
export type SignOutState =
    | { status: "idle" }
    | { status: "pending"; keepInvitation: boolean }
    | { status: "failed"; keepInvitation: boolean; error: unknown };
type SessionState = {
    session?: Session;
    ended: boolean;
    changed: boolean;
    replaced: boolean;
    signOut: SignOutState;
};
type Confirmation = {
    sentAt: number;
    covered: boolean;
};

// PYRACAST_SESSION_IDLE_SECONDS in settings.py: the server ends a session left this long.
const serverIdleLimitMs = 30 * 60_000;
const clockTickMs = 30_000;

// The last session tag any tab of this browser observed. A write is an observation of a session
// read, never a new value, so tabs that read crossing answers converge.
export const observedTagKey = "pyracast.sessionTag";
// The signed-in owner this tab's document last showed, and how a sign-out begun here treats the
// pending invitation. The next document compares its first answer with it.
const shownOwnerKey = "pyracast.shownOwner";
type ShownOwner = {
    owner: string;
    signOut?: "keepInvitation" | "clearInvitation";
};
type PreviousEnd = { notice?: "ended" | "changed"; clearInvitation: boolean };

let state: SessionState = {
    ended: false,
    changed: false,
    replaced: false,
    signOut: { status: "idle" },
};
let confirmation: Confirmation = { sentAt: 0, covered: false };
let documentOwner: string | null | undefined;
// What a session read's answer may have missed: this tab's session changes, another tab's
// observation and every read started or session adopted here.
type Mark = { epoch: number; observed: string | null; generation: number };
type Flight = Mark & { promise: Promise<Session> };

let flight: Flight | undefined;
let generation = 0;
let adopted: { mark: Mark; session: Session } | undefined;
let confirming: Promise<Tag> | undefined;
let signingOut = false;
let clearTabInvitation = () => {};
const listeners = new Set<() => void>();

export function sessionState() {
    return state;
}

function unconfirmed() {
    return confirmation.covered;
}

function subscribeSession(listener: () => void) {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

function notify() {
    for (const listener of listeners) listener();
}

export function useSessionState() {
    return useSyncExternalStore(subscribeSession, sessionState, sessionState);
}

export function useSessionUnconfirmed() {
    return useSyncExternalStore(subscribeSession, unconfirmed, unconfirmed);
}

export function useSessionClock() {
    const [now, setNow] = useState(Date.now);
    useEffect(() => {
        const tick = () => setNow(Date.now());
        const timer = window.setInterval(tick, clockTickMs);
        document.addEventListener("visibilitychange", tick);
        return () => {
            window.clearInterval(timer);
            document.removeEventListener("visibilitychange", tick);
        };
    }, []);
    return now;
}

// The wall clock counts a suspended device's sleep; a send time later than now is old, so a clock set back cannot postpone the check.
function staleSince(sentAt: number) {
    const now = Date.now();
    return now < sentAt || now - sentAt >= serverIdleLimitMs;
}

function stale() {
    return staleSince(confirmation.sentAt);
}

// A stale stamp, also one from before the clock was set back, yields to any fresh answer.
function confirm(sentAt: number) {
    if (staleSince(sentAt) || (!stale() && sentAt < confirmation.sentAt))
        return false;
    confirmation = { sentAt, covered: false };
    return true;
}

export function confirmAnswer(sentAt: number) {
    if (confirm(sentAt)) notify();
}

// Once set, the cover stays whatever the clock does until an answer arrives.
function failConfirmation() {
    if (confirmation.covered || !stale()) return;
    confirmation = { ...confirmation, covered: true };
    notify();
}

export function confirmIdleSession() {
    if (state.session?.user && stale()) loadSession().catch(() => {});
}

// The tab's pending invitation lives in a feature module, which this store cannot import.
export function clearInvitationWith(clear: () => void) {
    clearTabInvitation = clear;
}

function readShownOwner(): ShownOwner | undefined {
    try {
        const value: unknown = JSON.parse(
            window.sessionStorage.getItem(shownOwnerKey) ?? "null",
        );
        return isShownOwner(value) ? value : undefined;
    } catch {
        return undefined;
    }
}

function isShownOwner(value: unknown): value is ShownOwner {
    if (typeof value !== "object" || value === null) return false;
    const { owner, signOut } = value as Record<string, unknown>;
    return (
        typeof owner === "string" &&
        (signOut === undefined ||
            signOut === "keepInvitation" ||
            signOut === "clearInvitation")
    );
}

function recordShownOwner(shown: ShownOwner | undefined) {
    try {
        if (shown)
            window.sessionStorage.setItem(shownOwnerKey, JSON.stringify(shown));
        else window.sessionStorage.removeItem(shownOwnerKey);
    } catch {
        return;
    }
}

// Another first owner than the one the tab last showed means that session ended while no
// document looked, unless a sign-out began here.
function previousEnd(owner: string | null): PreviousEnd | undefined {
    const shown = readShownOwner();
    recordShownOwner(owner === null ? undefined : { owner });
    if (!shown || shown.owner === owner) return undefined;
    if (shown.signOut)
        return { clearInvitation: shown.signOut === "clearInvitation" };
    return {
        notice: owner === null ? "ended" : "changed",
        clearInvitation: true,
    };
}

function tagOf(session: Session | undefined): Tag {
    return session && (session.session_tag ?? "");
}

// The signed-in session that cached server state and mounted screens belong to.
export function sessionOwner(session: Session | undefined) {
    return session?.user ? `${session.user.id}:${tagOf(session)}` : undefined;
}

function observedTag(): string | null {
    try {
        return window.localStorage.getItem(observedTagKey);
    } catch {
        return null;
    }
}

function observe(tag: string) {
    try {
        if (window.localStorage.getItem(observedTagKey) !== tag)
            window.localStorage.setItem(observedTagKey, tag);
    } catch {
        // Without storage a tab learns another tab's change at its next answer or focus read.
    }
}

export function isCurrent(session: Session | undefined) {
    const observed = observedTag();
    return observed === null || observed === tagOf(session);
}

export function adoptSession(session: Session, sentAt?: number) {
    const owner = sessionOwner(session) ?? null;
    if (documentOwner === undefined) {
        documentOwner = owner;
        const ended = previousEnd(owner);
        if (ended?.clearInvitation) clearTabInvitation();
        state = {
            ...state,
            session,
            ended: ended?.notice === "ended",
            changed: ended?.notice === "changed",
        };
    } else
        state = {
            ...state,
            session,
            replaced: state.replaced || owner !== documentOwner,
        };
    if (sentAt !== undefined) confirm(sentAt);
    setCsrfToken(session.csrf_token);
    observe(session.session_tag ?? "");
    generation += 1;
    adopted = { mark: mark(), session };
    notify();
}

function mark(): Mark {
    return { epoch: mutationEpoch(), observed: observedTag(), generation };
}

function unchangedSince(since: Mark) {
    const now = mark();
    return (
        since.epoch === now.epoch &&
        since.observed === now.observed &&
        since.generation === now.generation
    );
}

async function readSession() {
    try {
        return await send<Session>({ method: "GET", url: "/api/session/" });
    } catch (error) {
        failConfirmation();
        throw error;
    }
}

// A read that carried a key another tab's confirmation replaced meanwhile shows no session; the
// tab adopts an ended session only when a second read sent after it agrees.
async function confirmedRead(): Promise<Session> {
    const session = await readSession();
    if (session.user || !state.session?.user || signingOut) return session;
    return readSession();
}

// A read is shared while neither this tab sent a session change nor another tab observed a tag
// since it started. It adopts its answer only when nothing changed while it ran; otherwise it
// answers with a session adopted after the change, or with a read started after it.
function startRead(): Flight {
    generation += 1;
    const started = mark();
    const sentAt = Date.now();
    const current: Flight = {
        ...started,
        promise: confirmedRead().then((session) => {
            if (unchangedSince(started)) {
                adoptSession(session, sentAt);
                return session;
            }
            if (flight === current) flight = undefined;
            return adopted && unchangedSince(adopted.mark)
                ? adopted.session
                : loadSession();
        }),
    };
    flight = current;
    current.promise
        .finally(() => {
            if (flight === current) flight = undefined;
        })
        .catch(() => {});
    return current;
}

export function loadSession(): Promise<Session> {
    if (
        flight &&
        flight.epoch === mutationEpoch() &&
        flight.observed === observedTag()
    )
        return flight.promise;
    return startRead().promise;
}

function setSignOut(signOut: SignOutState) {
    state = { ...state, signOut };
    notify();
}

// One sign-out request, for the session this tab adopted; when another tab changed the session
// meanwhile, the server refuses it and the tab shows the session the browser now holds.
export async function signOut({ keepInvitation = false } = {}) {
    const owner = sessionOwner(state.session);
    const shown = owner !== undefined;
    if (shown) {
        recordShownOwner({
            owner,
            signOut: keepInvitation ? "keepInvitation" : "clearInvitation",
        });
        setSignOut({ status: "pending", keepInvitation });
    }
    signingOut = true;
    try {
        await authRequest("auth/session", "DELETE");
        const session = await loadSession();
        if (session.user) throw changedSession();
    } catch (error) {
        if (shown) setSignOut({ status: "failed", keepInvitation, error });
        throw error;
    } finally {
        signingOut = false;
    }
}

// A document whose session was replaced sends nothing tagged before its reload; the session read
// it may still need is never tagged.
function documentTag(): Tag {
    if (state.replaced) throw changedSession();
    return tagOf(state.session);
}

// Session reads queue behind the session changes of this tab sent before them, so a read started
// after a refused answer, and shared by the answers arriving while it runs, sees any key that
// replaced the one those requests carried.
confirmSessionWith({
    tag: documentTag,
    confirm: () => {
        confirming ??= startRead()
            .promise.then(tagOf)
            .finally(() => {
                confirming = undefined;
            });
        return confirming;
    },
});
