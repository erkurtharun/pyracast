import { useSyncExternalStore } from "react";
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
type SessionState = { session?: Session; ended: boolean };

// The last session tag any tab of this browser observed. A write is an observation of a session
// read, never a new value, so tabs that read crossing answers converge.
export const observedTagKey = "pyracast.sessionTag";

let state: SessionState = { ended: false };
// What a session read's answer may have missed: this tab's session changes, another tab's
// observation and every read started or session adopted here.
type Mark = { epoch: number; observed: string | null; generation: number };
type Flight = Mark & { promise: Promise<Session> };

let flight: Flight | undefined;
let generation = 0;
let adopted: { mark: Mark; session: Session } | undefined;
let confirming: Promise<Tag> | undefined;
let signingOut = false;
const listeners = new Set<() => void>();

export function sessionState() {
    return state;
}

function subscribeSession(listener: () => void) {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

export function useSessionState() {
    return useSyncExternalStore(subscribeSession, sessionState, sessionState);
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

export function adoptSession(session: Session) {
    state = {
        session,
        ended: session.user
            ? false
            : !signingOut &&
              (state.ended ||
                  confirming !== undefined ||
                  Boolean(state.session?.user)),
    };
    setCsrfToken(session.csrf_token);
    observe(session.session_tag ?? "");
    generation += 1;
    adopted = { mark: mark(), session };
    for (const listener of listeners) listener();
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

const readSession = () =>
    send<Session>({ method: "GET", url: "/api/session/" });

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
    const current: Flight = {
        ...started,
        promise: confirmedRead().then((session) => {
            if (unchangedSince(started)) {
                adoptSession(session);
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

// One sign-out request, for the session this tab adopted; when another tab changed the session
// meanwhile, the server refuses it and the tab shows the session the browser now holds.
export async function signOut() {
    signingOut = true;
    try {
        await authRequest("auth/session", "DELETE");
        const session = await loadSession();
        if (session.user) throw changedSession();
        return session;
    } finally {
        signingOut = false;
    }
}

// Session reads queue behind the session changes of this tab sent before them, so a read started
// after a refused answer, and shared by the answers arriving while it runs, sees any key that
// replaced the one those requests carried.
confirmSessionWith({
    tag: () => tagOf(state.session),
    confirm: () => {
        confirming ??= startRead()
            .promise.then(tagOf)
            .finally(() => {
                confirming = undefined;
            });
        return confirming;
    },
});
