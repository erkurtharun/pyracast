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
export type SessionState = { session?: Session; ended: boolean };

// The last session tag any tab of this browser observed. A write is an observation of a session
// read, never a new value, so tabs that read crossing answers converge.
export const observedTagKey = "pyracast.sessionTag";

let state: SessionState = { ended: false };
let flight: { epoch: number; promise: Promise<Session> } | undefined;
let confirming: Promise<Tag> | undefined;
let signingOut = false;
const listeners = new Set<() => void>();

export function sessionState() {
    return state;
}

export function subscribeSession(listener: () => void) {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

export function useSessionState() {
    return useSyncExternalStore(subscribeSession, sessionState, sessionState);
}

export function tagOf(session: Session | undefined): Tag {
    return session && (session.session_tag ?? "");
}

// The signed-in session that cached server state and mounted screens belong to.
export function sessionOwner(session: Session | undefined) {
    return session?.user ? `${session.user.id}:${tagOf(session)}` : undefined;
}

export function observedTag(): string | null {
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

// A tab is current while the last tag observed in this browser is the one it adopted.
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
    for (const listener of listeners) listener();
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

export function loadSession(): Promise<Session> {
    const epoch = mutationEpoch();
    if (flight?.epoch === epoch) return flight.promise;
    const promise = confirmedRead().then((session) => {
        adoptSession(session);
        return session;
    });
    const current = { epoch, promise };
    flight = current;
    promise
        .finally(() => {
            if (flight === current) flight = undefined;
        })
        .catch(() => {});
    return promise;
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
        confirming ??= confirmedRead()
            .then((session) => {
                adoptSession(session);
                return tagOf(session);
            })
            .finally(() => {
                confirming = undefined;
            });
        return confirming;
    },
});
