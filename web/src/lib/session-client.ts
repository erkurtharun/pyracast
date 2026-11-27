import { useEffect, useState, useSyncExternalStore } from "react";
import type { components } from "./schema";
import {
    changedSession,
    confirmSessionWith,
    mutationEpoch,
    send,
    setCsrfToken,
    type LaneRequest,
    type Tag,
} from "./api/lane";
import { ApiError, NotSentError } from "./api/errors";
import { authRequest, authUrl, type AuthResult } from "./auth-api";
import { browserErrors } from "@/i18n/browser";
import { deadline, type Deadline } from "./saved/deadline";
import { SAVED_FIGURES } from "./saved/feature";
import type { CopyState } from "./saved/store";

export type Session = components["schemas"]["SessionOutput"];
export type SessionUser = components["schemas"]["SessionUserOutput"];
export type SignOutState =
    | { status: "idle" }
    | { status: "pending"; keepInvitation: boolean }
    | { status: "failed"; keepInvitation: boolean; error: unknown };
type Retirement = "signedOutHere" | "otherOwner";
type SessionState = {
    session?: Session;
    ended: boolean;
    changed: boolean;
    retired?: Retirement;
    signOut: SignOutState;
};
type Confirmation = {
    sentAt: number;
    covered: boolean;
};

const clockTickMs = 30_000;

// The last session tag any tab of this browser observed. A write is an observation of a session
// read, never a new value, so tabs that read crossing answers converge.
export const observedTagKey = "pyracast.sessionTag";
// Changes at every change of the observed tag, also one that returns to an earlier tag.
const observedRevisionKey = "pyracast.sessionRevision";
// The signed-in owner this tab's document last showed, and how a sign-out begun here treats the
// pending invitation. The next document compares its first answer with it.
const shownOwnerKey = "pyracast.shownOwner";
type ShownOwner = {
    owner: string;
    signOut?: "keepInvitation" | "clearInvitation";
};
type PreviousEnd = { notice?: "ended" | "changed"; clearInvitation: boolean };
// A start and a hand-over consult the store of saved figures only where this flag is set or cannot be read.
export const savedFlagKey = "pyracast.saved";
const savedChangeKey = "pyracast.savedChange";
const savedLock = "pyracast.saved-copy";
const savedLimitMs = 2_000;
// A sign-out sends its request after two seconds; its record may still land until the document reloads after ten.
const recordLimitMs = 10_000;
export type SavedStore = typeof import("./saved/store");
type Handoff = "handed" | "superseded" | "unhanded";

let state: SessionState = {
    ended: false,
    changed: false,
    signOut: { status: "idle" },
};
let confirmation: Confirmation = { sentAt: 0, covered: false };
let documentOwner: string | null | undefined;
// What a session read's answer may have missed: this tab's session changes, another tab's
// observation and every read started or session adopted here.
type Mark = { epoch: number; observed: string; generation: number };
type Flight = Mark & { promise: Promise<Session> };

let flight: Flight | undefined;
let generation = 0;
let adopted: { mark: Mark; session: Session } | undefined;
let confirming: Promise<Tag> | undefined;
let signingOut = false;
let clearTabInvitation = () => {};
let savedStore: Promise<SavedStore> | undefined;
let savedWork: Promise<unknown> = Promise.resolve();
const pendingAnswers = new Set<Promise<unknown>>();
let handoffFailed = false;
const listeners = new Set<() => void>();
const handoffListeners = new Set<() => void>();
const savedListeners = new Set<() => void>();

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

function subscribeHandoff(listener: () => void) {
    handoffListeners.add(listener);
    return () => {
        handoffListeners.delete(listener);
    };
}

function handoffFailure() {
    return handoffFailed;
}

function setHandoffFailed(failed: boolean) {
    if (handoffFailed === failed) return;
    handoffFailed = failed;
    for (const listener of handoffListeners) listener();
}

export function useHandoffFailed() {
    return useSyncExternalStore(subscribeHandoff, handoffFailure, () => false);
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

// The server's idle limit of the shown session; a session without a user has none.
function idleLimitMs() {
    return (state.session?.idle_seconds ?? Infinity) * 1000;
}

// The wall clock counts a suspended device's sleep; a send time later than now is old, so a clock set back cannot postpone the check.
function staleSince(sentAt: number) {
    const now = Date.now();
    return now < sentAt || now - sentAt >= idleLimitMs();
}

function stale() {
    return staleSince(confirmation.sentAt);
}

// A stale stamp, also one from before the clock was set back, yields to any fresh answer.
function confirm(sentAt: number) {
    if (staleSince(sentAt)) return false;
    confirmation = {
        sentAt: stale() ? sentAt : Math.max(sentAt, confirmation.sentAt),
        covered: false,
    };
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
    if (state.session?.user && !handoffFailed && stale())
        loadSession().catch(() => {});
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

function observedRevision() {
    return readStored(observedRevisionKey) ?? null;
}

function observe(tag: string) {
    try {
        if (window.localStorage.getItem(observedTagKey) === tag) return;
        window.localStorage.setItem(observedRevisionKey, randomHex());
        window.localStorage.setItem(observedTagKey, tag);
    } catch {
        // Without storage a tab learns another tab's change at its next answer or focus read.
    }
}

export function isCurrent(session: Session | undefined) {
    const observed = observedTag();
    return observed === null || observed === tagOf(session);
}

export function readStored(key: string): string | null | undefined {
    try {
        return window.localStorage.getItem(key);
    } catch {
        return undefined;
    }
}

export function writeStored(key: string, value: string | null) {
    try {
        if (value === null) window.localStorage.removeItem(key);
        else window.localStorage.setItem(key, value);
    } catch {
        return;
    }
}

function savedCopyFlagged() {
    return readStored(savedFlagKey) !== null;
}

// A flag written anew since the deletion was asked for names another copy.
function forgetSavedCopy(flag: string | null | undefined) {
    if (readStored(savedFlagKey) === flag) writeStored(savedFlagKey, null);
}

function randomHex() {
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
        byte.toString(16).padStart(2, "0"),
    ).join("");
}

function loadedStore<T>(work: (store: SavedStore) => Promise<T>) {
    savedStore ??= import("./saved/store");
    return savedStore.then(work).catch(() => {
        savedStore = undefined;
        return undefined;
    });
}

// The copy's operations run one after another, in the order this document asked for them.
function withSavedStore<T>(
    work: (store: SavedStore) => Promise<T>,
): Promise<T | undefined> {
    const next = savedWork.then(() => loadedStore(work));
    savedWork = next;
    return next;
}

// A browser without Web Locks has one window's order only.
async function underLock<T>(
    limit: Deadline,
    work: () => Promise<T>,
): Promise<T> {
    const locks =
        typeof navigator === "undefined" ? undefined : navigator.locks;
    if (!locks) return work();
    const waiting = new AbortController();
    void limit.reached.then(() => waiting.abort());
    let result!: T;
    await locks.request(savedLock, { signal: waiting.signal }, async () => {
        result = await work();
    });
    return result;
}

// One deadline from admission bounds the wait in the queue, the store's load, the lock and its work; work of a single transaction may skip the queue and the lock.
export async function boundedStore<T>(
    work: (store: SavedStore, limit: Deadline) => Promise<T>,
    limit = deadline(savedLimitMs),
    ordered = true,
): Promise<T | undefined> {
    const answer = Promise.race([
        ordered
            ? withSavedStore((store) =>
                  underLock(limit, () => work(store, limit)),
              )
            : loadedStore((store) => work(store, limit)),
        limit.reached.then(() => undefined),
    ]);
    pendingAnswers.add(answer);
    try {
        return await answer;
    } finally {
        pendingAnswers.delete(answer);
        limit.clear();
    }
}

export async function savedCopyAnswered() {
    await Promise.all(pendingAnswers);
}

export function startSavedCopy() {
    if (SAVED_FIGURES || !savedCopyFlagged()) return;
    const flag = readStored(savedFlagKey);
    void boundedStore((store, limit) =>
        store.removeCopy(limit, () => forgetSavedCopy(flag)),
    );
}

function handOffNeeded(session: Session) {
    return Boolean(session.user) && savedCopyFlagged();
}

// A browser that refuses storage outright, the flag included, can hold no copy.
async function handOff(
    { user, language, session_tag }: Session,
    present: () => boolean,
    adoptedAt: number,
): Promise<Handoff> {
    if (!user) return "handed";
    const flagUnreadable = readStored(savedFlagKey) === undefined;
    const limit = deadline(savedLimitMs);
    const handoff = await boundedStore(
        (store) =>
            store.adopt(
                {
                    user: user.id,
                    session: session_tag ?? "",
                    language,
                    adoptedAt,
                    present,
                },
                limit,
            ),
        limit,
    );
    if (handoff === "superseded") return "superseded";
    if (limit.expired()) return "unhanded";
    return handoff === "verified" || (handoff === "blocked" && flagUnreadable)
        ? "handed"
        : "unhanded";
}

// Recorded before the request, also without a copy: a window opened meanwhile can keep nothing for this session.
async function endSavedSession(session: Session | undefined) {
    const tag = session?.session_tag;
    if (!SAVED_FIGURES || !session?.user || !tag) return;
    const leaving = { user: session.user.id, session: tag };
    const waited = deadline(savedLimitMs);
    await Promise.race([
        boundedStore(
            (store, limit) =>
                store.endSession(leaving, announceSavedChange, limit),
            deadline(recordLimitMs),
            false,
        ),
        waited.reached,
    ]);
    waited.clear();
}

export type SavedFigures = { state: CopyState; plants: number };

export async function savedCopyState(
    session = state.session,
): Promise<SavedFigures> {
    if (!SAVED_FIGURES || !savedCopyFlagged())
        return { state: "off", plants: 0 };
    const owner = {
        user: session?.user?.id ?? 0,
        session: session?.session_tag ?? "",
    };
    return (
        (await boundedStore((store, limit) =>
            store.copyState(owner, limit),
        )) ?? {
            state: "unavailable",
            plants: 0,
        }
    );
}

export function sessionStill(owner: string) {
    return (
        sessionOwner(state.session) === owner &&
        isCurrent(state.session) &&
        state.retired === undefined &&
        state.signOut.status === "idle"
    );
}

export function announceSavedChange() {
    writeStored(savedChangeKey, randomHex());
    for (const listener of savedListeners) listener();
}

// The flag is written before the copy exists, so another person's sign-in always finds a copy it must clear.
export async function turnOnSavedFigures() {
    const { session } = state;
    const owner = sessionOwner(session);
    const user = session?.user;
    const tag = session?.session_tag;
    if (!session || !owner || !user || !tag) return;
    if (readStored(savedFlagKey) === null)
        writeStored(savedFlagKey, randomHex());
    if (!readStored(savedFlagKey)) return;
    const revision = observation();
    await boundedStore((store, limit) =>
        store.turnOn(
            {
                user: user.id,
                session: tag,
                language: session.language,
                removal: randomHex(),
            },
            () => sessionStill(owner) && observation() === revision,
            announceSavedChange,
            limit,
        ),
    );
}

// Every key of the kept figures starts with the flag's name.
export function subscribeSavedFigures(listener: () => void) {
    const changed = (event: StorageEvent) => {
        if (event.key?.startsWith(savedFlagKey) !== false) listener();
    };
    const shown = () => {
        if (document.visibilityState === "visible") listener();
    };
    savedListeners.add(listener);
    window.addEventListener("storage", changed);
    document.addEventListener("visibilitychange", shown);
    return () => {
        savedListeners.delete(listener);
        window.removeEventListener("storage", changed);
        document.removeEventListener("visibilitychange", shown);
    };
}

class HandoffFailure extends Error {}

export function isHandoffFailure(error: unknown) {
    return error instanceof HandoffFailure;
}

function failHandoff() {
    setHandoffFailed(true);
    return new HandoffFailure(
        "The figures kept on this device could not be checked.",
    );
}

// Superseded: a newer adoption or read was observed meanwhile, so only a new read tells what the server holds.
export type Adoption = "handed" | "failed" | "superseded";

export function adoptSession(
    session: Session,
    sentAt?: number,
): Promise<Adoption> {
    if (!handOffNeeded(session)) {
        applySession(session, sentAt);
        return Promise.resolve("handed");
    }
    return adoptHandedOver(session, sentAt);
}

async function adoptHandedOver(
    session: Session,
    sentAt?: number,
): Promise<Adoption> {
    const since = mark();
    const handoff = await handOff(
        session,
        () => unchangedSince(since),
        Date.now(),
    );
    if (!unchangedSince(since) || handoff === "superseded") return "superseded";
    if (handoff === "unhanded") {
        failHandoff();
        return "failed";
    }
    applySession(session, sentAt);
    return "handed";
}

function applySession(session: Session, sentAt?: number) {
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
            retired:
                state.retired ??
                (owner === documentOwner ? undefined : "otherOwner"),
        };
    if (sentAt !== undefined) confirm(sentAt);
    setCsrfToken(session.csrf_token);
    observe(session.session_tag ?? "");
    generation += 1;
    adopted = { mark: mark(), session };
    setHandoffFailed(false);
    notify();
}

function observation() {
    return JSON.stringify([observedRevision(), observedTag()]);
}

function mark(): Mark {
    return { epoch: mutationEpoch(), observed: observation(), generation };
}

function unchangedSince(since: Mark) {
    const now = mark();
    return (
        since.epoch === now.epoch &&
        since.observed === now.observed &&
        since.generation === now.generation
    );
}

function shownAgain(session: Session) {
    const owner = sessionOwner(session);
    return owner !== undefined &&
        !handoffFailed &&
        sessionStill(owner) &&
        adopted?.mark.observed === observation()
        ? owner
        : undefined;
}

// Equal owner and revision leave a removal mark, the language and the 90 days to judge, so the hand-over still runs behind the screen.
function renewHandoff(session: Session, owner: string) {
    if (!handOffNeeded(session)) return;
    const observed = observation();
    void handOff(
        session,
        () => sessionStill(owner) && observation() === observed,
        Date.now(),
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
        promise: confirmedRead().then(async (session) => {
            const again = shownAgain(session);
            const handoff =
                handOffNeeded(session) && again === undefined
                    ? await handOff(
                          session,
                          () => unchangedSince(started),
                          Date.now(),
                      )
                    : "handed";
            if (unchangedSince(started)) {
                if (handoff === "unhanded") throw failHandoff();
                if (handoff === "handed") {
                    applySession(session, sentAt);
                    if (again !== undefined) renewHandoff(session, again);
                    return session;
                }
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
        flight.observed === observation()
    )
        return flight.promise;
    return startRead().promise;
}

function setSignOut(signOut: SignOutState) {
    state = { ...state, signOut };
    notify();
}

const signOutPath = "auth/session";
const signOutUrl = authUrl(signOutPath);

// allauth answers a completed sign-out with its anonymous 401.
function signedOutBy(answer: AuthResult) {
    return answer.status === 401 && answer.meta?.is_authenticated === false;
}

// One sign-out request, for the session this tab adopted; when another tab changed the session
// meanwhile, the server refuses it and the tab shows the session the browser now holds.
export async function signOut({ keepInvitation = false } = {}) {
    const leaving = state.session;
    const owner = sessionOwner(leaving);
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
        if (shown) await endSavedSession(leaving);
        const ended = await authRequest(signOutPath, "DELETE");
        if (!signedOutBy(ended))
            throw new ApiError(browserErrors()("invalidResponse"));
        if (shown) {
            state = { ...state, retired: state.retired ?? "signedOutHere" };
            notify();
            return;
        }
        const session = await loadSession();
        if (session.user) throw changedSession();
    } catch (error) {
        if (shown) setSignOut({ status: "failed", keepInvitation, error });
        throw error;
    } finally {
        signingOut = false;
    }
}

function documentTag(): Tag {
    return tagOf(state.session);
}

function shows(tag: string) {
    return state.retired === undefined && tagOf(state.session) === tag;
}

function notSent() {
    return new NotSentError(browserErrors()("notSentSigningOut"));
}

// Work admitted before the sign-out began can still reach the send while the sign-out is pending.
function refusal(request: LaneRequest, tag: string) {
    if (state.retired === "signedOutHere") return notSent();
    if (state.retired === "otherOwner" || tagOf(state.session) !== tag)
        return changedSession();
    if (signingOut && !isSignOut(request)) return notSent();
    return undefined;
}

function isSignOut({ method, url }: LaneRequest) {
    return method === "DELETE" && url === signOutUrl;
}

// Session reads queue behind the session changes of this tab sent before them, so a read started
// after a refused answer, and shared by the answers arriving while it runs, sees any key that
// replaced the one those requests carried.
confirmSessionWith({
    tag: documentTag,
    shows,
    refusal,
    confirm: () => {
        if (handoffFailed)
            return Promise.reject(new HandoffFailure("Try again reads first."));
        confirming ??= startRead()
            .promise.then(tagOf)
            .finally(() => {
                confirming = undefined;
            });
        return confirming;
    },
});
