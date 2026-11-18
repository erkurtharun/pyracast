import { useSyncExternalStore } from "react";
import type { components } from "./schema";
import { mutationEpoch, onSessionEnded, send, setCsrfToken } from "./api/lane";
import { authRequest } from "./auth-api";

export type Session = components["schemas"]["SessionOutput"];
export type SessionUser = components["schemas"]["SessionUserOutput"];
export type SessionState = { session?: Session; ended: boolean };

let state: SessionState = { ended: false };
let flight: { epoch: number; promise: Promise<Session> } | undefined;
let ending = false;
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

export function adoptSession(session: Session) {
    state = {
        session,
        ended: session.user
            ? false
            : !signingOut &&
              (state.ended || ending || Boolean(state.session?.user)),
    };
    setCsrfToken(session.csrf_token);
    for (const listener of listeners) listener();
}

export function loadSession(): Promise<Session> {
    const epoch = mutationEpoch();
    if (flight?.epoch === epoch) return flight.promise;
    const promise = send<Session>({ method: "GET", url: "/api/session/" }).then(
        (session) => {
            adoptSession(session);
            return session;
        },
    );
    const current = { epoch, promise };
    flight = current;
    promise
        .finally(() => {
            if (flight === current) flight = undefined;
        })
        .catch(() => {});
    return promise;
}

export async function signOut() {
    await authRequest("auth/session", "DELETE");
    signingOut = true;
    try {
        return await loadSession();
    } finally {
        signingOut = false;
    }
}

onSessionEnded(() => {
    if (!state.session?.user || ending) return;
    ending = true;
    loadSession()
        .catch(() => {})
        .finally(() => {
            ending = false;
        });
});
