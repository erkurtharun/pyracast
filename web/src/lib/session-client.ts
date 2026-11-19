import { useSyncExternalStore } from "react";
import type { components } from "./schema";
import {
    confirmSessionWith,
    mutationEpoch,
    send,
    setCsrfToken,
} from "./api/lane";
import { ApiError } from "./api/errors";
import { authRequest } from "./auth-api";
import { browserErrors } from "@/i18n/browser";

export type Session = components["schemas"]["SessionOutput"];
export type SessionUser = components["schemas"]["SessionUserOutput"];
// signals counts the session changes other tabs announced; unconfirmed holds until a session read
// sent after the latest one answers.
export type SessionState = {
    session?: Session;
    ended: boolean;
    signals: number;
    unconfirmed: boolean;
};

let state: SessionState = { ended: false, signals: 0, unconfirmed: false };
let flight:
    { epoch: number; signal: number; promise: Promise<Session> } | undefined;
let confirming: Promise<boolean> | undefined;
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

function notify() {
    for (const listener of listeners) listener();
}

// Tabs share the session cookie, so a sign-in or sign-out in one tab tells the others that their
// session is unconfirmed; the message carries no account and no data.
const channel =
    typeof window !== "undefined" && "BroadcastChannel" in window
        ? new BroadcastChannel("pyracast-session")
        : undefined;
channel?.addEventListener("message", () => {
    state = { ...state, signals: state.signals + 1, unconfirmed: true };
    notify();
});

export function adoptSession(session: Session) {
    adopt(session, false);
}

function adopt(session: Session, confirmed: boolean) {
    const previous = state;
    state = {
        session,
        ended: session.user
            ? false
            : !signingOut &&
              (state.ended ||
                  confirming !== undefined ||
                  Boolean(state.session?.user)),
        signals: state.signals,
        unconfirmed: state.unconfirmed && !confirmed,
    };
    setCsrfToken(session.csrf_token);
    notify();
    if (
        previous.session &&
        !previous.unconfirmed &&
        previous.session.user?.id !== session.user?.id
    )
        channel?.postMessage("changed");
}

export function loadSession(): Promise<Session> {
    const epoch = mutationEpoch();
    const signal = state.signals;
    if (flight?.epoch === epoch && flight.signal === signal)
        return flight.promise;
    const promise = send<Session>({ method: "GET", url: "/api/session/" }).then(
        (session) => {
            adopt(session, signal === state.signals);
            return session;
        },
    );
    const current = { epoch, signal, promise };
    flight = current;
    promise
        .finally(() => {
            if (flight === current) flight = undefined;
        })
        .catch(() => {});
    return promise;
}

async function endSession() {
    await authRequest("auth/session", "DELETE");
    signingOut = true;
    try {
        return await loadSession();
    } finally {
        signingOut = false;
    }
}

// A sign-out that carried a key another tab replaced meanwhile ends nothing and leaves the newer
// key in place; the session read after it shows that key, which is signed out once more.
export async function signOut() {
    const session = await endSession();
    if (!session.user) return session;
    const repeated = await endSession();
    if (!repeated.user) return repeated;
    throw new ApiError(browserErrors()("stillSignedIn"));
}

// Session reads queue behind the session changes of this tab sent before them, so a read started
// after a signed-out answer, and shared by the answers arriving while it runs, sees any key that
// replaced the one those requests carried.
confirmSessionWith(() => {
    if (!state.session?.user) return Promise.resolve(false);
    confirming ??= send<Session>({ method: "GET", url: "/api/session/" })
        .then((session) => {
            adoptSession(session);
            return Boolean(session.user);
        })
        .finally(() => {
            confirming = undefined;
        });
    return confirming;
});
