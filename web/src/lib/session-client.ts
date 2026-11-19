import { useSyncExternalStore } from "react";
import type { components } from "./schema";
import {
    confirmedSession,
    confirmSession,
    confirmSessionWith,
    mutationEpoch,
    onStaleSession,
    send,
    sessionCurrent,
    setCsrfToken,
} from "./api/lane";
import {
    storedGeneration,
    writeGeneration,
    type Generation,
} from "./api/generation";
import { ApiError } from "./api/errors";
import { authRequest } from "./auth-api";
import { browserErrors } from "@/i18n/browser";

export type Session = components["schemas"]["SessionOutput"];
export type SessionUser = components["schemas"]["SessionUserOutput"];
// current holds while the browser's session generation is the one this tab confirmed; failure is
// the last session read that failed while no session could be shown.
export type SessionState = {
    session?: Session;
    ended: boolean;
    current: boolean;
    failure?: string;
};

type Read = {
    epoch: number;
    generation: Generation;
    confirms: boolean;
    promise: Promise<Session>;
};

let state: SessionState = { ended: false, current: true };
let running: Read | undefined;
let queued: { promise: Promise<Session>; start: () => void } | undefined;
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

function update(next: Partial<SessionState>) {
    state = { ...state, ...next };
    for (const listener of listeners) listener();
}

export function adoptSession(session: Session) {
    setCsrfToken(session.csrf_token);
    update({
        session,
        ended: session.user
            ? false
            : !signingOut && (state.ended || Boolean(state.session?.user)),
    });
}

// The generation read just before the read was sent is the one it confirms, so a change during the
// read leaves the tab not current and causes one more read.
function adoptRead(session: Session, generation: Generation) {
    const owner = session.user?.id ?? null;
    const previous = confirmedSession();
    // A change this tab finds while current was announced by no other tab.
    if (
        previous &&
        previous.owner !== owner &&
        previous.generation === generation
    )
        writeGeneration();
    confirmSession({ generation, owner });
    state = { ...state, current: sessionCurrent(), failure: undefined };
    adoptSession(session);
    if (!state.current) loadSession().catch(() => {});
}

export function checkSession() {
    const current = sessionCurrent();
    if (current !== state.current) update({ current });
    if (!current) loadSession().catch(() => {});
}

onStaleSession(checkSession);

function start(confirms: boolean): Promise<Session> {
    const epoch = mutationEpoch();
    const generation = storedGeneration();
    if (state.failure) update({ failure: undefined });
    const promise = send<Session>({ method: "GET", url: "/api/session/" }).then(
        (session) => {
            adoptRead(session, generation);
            return session;
        },
        (error: Error) => {
            if (!state.session || !state.current)
                update({ failure: error.message });
            throw error;
        },
    );
    running = { epoch, generation, confirms, promise };
    const next = () => {
        running = undefined;
        const waiting = queued;
        queued = undefined;
        waiting?.start();
    };
    promise.then(next, next);
    return promise;
}

// At most one session read is in flight and one waits behind it. A running read is shared while no
// change of this tab was sent and the generation stayed since it left; a confirmation after a
// refused answer shares only a read that a confirmation started.
function readSession(confirms: boolean): Promise<Session> {
    if (!running) return start(confirms);
    if (
        running.epoch === mutationEpoch() &&
        running.generation === storedGeneration() &&
        (running.confirms || !confirms)
    )
        return running.promise;
    if (!queued) {
        let begin!: () => void;
        const promise = new Promise<Session>((resolve, reject) => {
            begin = () => start(true).then(resolve, reject);
        });
        queued = { promise, start: begin };
    }
    return queued.promise;
}

export function loadSession(): Promise<Session> {
    return readSession(false);
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
confirmSessionWith(async () => {
    if (!state.session?.user) return null;
    return (await readSession(true)).user?.id ?? null;
});
