import { useSyncExternalStore } from "react";
import type { components } from "./schema";
import {
    confirmSessionWith,
    mutationEpoch,
    send,
    setCsrfToken,
    type Owner,
} from "./api/lane";
import { ApiError } from "./api/errors";
import { authRequest } from "./auth-api";
import { browserErrors } from "@/i18n/browser";

export type Session = components["schemas"]["SessionOutput"];
export type SessionUser = components["schemas"]["SessionUserOutput"];
export type SessionState = { session?: Session; ended: boolean };

let state: SessionState = { ended: false };
let flight: { epoch: number; promise: Promise<Session> } | undefined;
let confirming: Promise<Owner> | undefined;
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
              (state.ended ||
                  confirming !== undefined ||
                  Boolean(state.session?.user)),
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
// key in place; the session read after it shows that key, which is signed out once more only
// while it belongs to the account this tab signed out.
export async function signOut() {
    const owner = state.session?.user?.id;
    const session = await endSession();
    if (!session.user) return session;
    if (session.user.id === owner) {
        const repeated = await endSession();
        if (!repeated.user) return repeated;
    }
    throw new ApiError(browserErrors()("stillSignedIn"));
}

// Session reads queue behind the session changes of this tab sent before them, so a read started
// after a signed-out answer, and shared by the answers arriving while it runs, sees any key that
// replaced the one those requests carried.
confirmSessionWith({
    owner: () => state.session?.user?.id,
    confirm: () => {
        if (!state.session?.user) return Promise.resolve(undefined);
        confirming ??= send<Session>({ method: "GET", url: "/api/session/" })
            .then((session) => {
                adoptSession(session);
                return session.user?.id;
            })
            .finally(() => {
                confirming = undefined;
            });
        return confirming;
    },
});
