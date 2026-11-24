import { expect, test } from "@playwright/test";
import { keys } from "../src/lib/api/keys";
import type { Session } from "../src/lib/session-client";
import { openDocument } from "./support/document";
import { errorBody } from "./support/errors";

let {
    adoptSession,
    answerLost,
    api,
    ApiError,
    authRequest,
    isCurrent,
    loadSession,
    NotSentError,
    observedTagKey,
    sessionOwner,
    sessionState,
    signOut,
} = openDocument();

function startDocument() {
    ({
        adoptSession,
        answerLost,
        api,
        ApiError,
        authRequest,
        isCurrent,
        loadSession,
        NotSentError,
        observedTagKey,
        sessionOwner,
        sessionState,
        signOut,
    } = openDocument());
}

type Call = { method: string; url: string; tag: string | null };
type Answer = Response | Promise<Response>;

const realFetch = globalThis.fetch;
const realWindow = globalThis.window;
let calls: Call[] = [];
let stored: Map<string, string>;

function json(body: unknown, status = 200) {
    return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}

function answer(...answers: ((call: Call) => Answer)[]) {
    globalThis.fetch = async (input, init) => {
        const call = {
            method: init?.method ?? "GET",
            url: input instanceof Request ? input.url : String(input),
            tag: new Headers(init?.headers).get("X-Session-Tag"),
        };
        const next = answers[calls.length];
        calls.push(call);
        if (!next) throw new Error("No answer is prepared for this request.");
        return next(call);
    };
}

function held() {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    return { gate, release };
}

function session(tag: string, id = 1): Session {
    return {
        user: {
            id,
            username: `user-${id}`,
            email: `user-${id}@example.test`,
            has_usable_password: true,
            can_admin: false,
            mfa_required: false,
            language_preference: null,
        },
        csrf_token: `token-${tag}`,
        language: "en",
        expires_at: "2026-11-21T00:00:00Z",
        session_tag: tag,
    };
}

const signedOut: Session = {
    user: null,
    csrf_token: "anonymous",
    language: "en",
    expires_at: null,
    session_tag: null,
};
const changed = () =>
    json(errorBody("session_changed", "The session changed."), 409);
const site = {
    path: { tenant_id: 1 },
    body: {
        name: "Plant",
        latitude: 36.9,
        longitude: 30.7,
        timezone: "Europe/Istanbul",
        dc_capacity_mw: 5,
        measurement_basis: "net_export",
        mounting_type: "fixed",
    },
} as const;
const sent = () =>
    calls.map((call) => `${call.method} ${call.url} ${call.tag}`);

test.beforeEach(() => {
    calls = [];
    stored = new Map();
    Object.assign(globalThis, {
        window: {
            localStorage: {
                getItem: (key: string) => stored.get(key) ?? null,
                setItem: (key: string, value: string) => stored.set(key, value),
            },
        },
    });
    startDocument();
});
test.afterEach(() => {
    globalThis.fetch = realFetch;
    Object.assign(globalThis, { window: realWindow });
});

test("U1: a save in flight while another session takes over is neither repeated nor applied", async () => {
    await adoptSession(session("a"));
    answer(changed, () => json(session("b", 2)));
    const refused = await api
        .post("/api/tenants/{tenant_id}/sites/", site)
        .catch((failure) => failure);
    expect(refused).toMatchObject({ code: "session_changed" });
    expect(sent()).toEqual([
        "POST /api/tenants/1/sites/ a",
        "GET /api/session/ null",
    ]);

    calls = [];
    startDocument();
    await adoptSession(session("b", 2));
    const { gate, release } = held();
    const { arrived, arrive } = arrival();
    answer(async () => {
        arrive();
        await gate;
        return json({ id: 7 }, 201);
    });
    const saving = api.post("/api/tenants/{tenant_id}/sites/", site);
    await arrived;
    await adoptSession(session("c", 3));
    release();
    const late = await saving.catch((failure) => failure);
    expect(late).toBeInstanceOf(ApiError);
    expect(late).toMatchObject({ code: "session_changed" });
    expect(sent()).toEqual(["POST /api/tenants/1/sites/ b"]);
});

test("U2: the same account signed in again owns none of the earlier session's cached data", () => {
    const earlier = sessionOwner(session("a"));
    const later = sessionOwner(session("a2"));
    expect(earlier).not.toBe(later);
    const path = "/api/tenants/{tenant_id}/sites/";
    const options = { path: { tenant_id: 5 } };
    expect(JSON.stringify(keys.request(String(later), path, options))).not.toBe(
        JSON.stringify(keys.request(String(earlier), path, options)),
    );
});

test("U3: a sign-out another document announced leaves this tab not current until it reads", async () => {
    await adoptSession(session("a"));
    expect(isCurrent(sessionState().session)).toBe(true);
    stored.set(observedTagKey, "");
    expect(isCurrent(sessionState().session)).toBe(false);
    answer(
        () => json(signedOut),
        () => json(signedOut),
    );
    await loadSession();
    expect(sessionState().session?.user).toBeNull();
    expect(isCurrent(sessionState().session)).toBe(true);
});

test("U4: a sign-in prepared before another tab signed in leaves this tab on the session the browser holds", async () => {
    await adoptSession(signedOut);
    answer(changed, () => json(session("a")));
    const refused = await authRequest("auth/login", "POST", {
        email: "user-2@example.test",
        password: "secret",
    }).catch((failure) => failure);
    expect(refused).toMatchObject({ code: "session_changed" });
    expect(sessionState().session?.user?.id).toBe(1);
    expect(sent()).toEqual([
        "POST /api/auth/browser/v1/auth/login ",
        "GET /api/session/ null",
    ]);
    expect(stored.get(observedTagKey)).toBe("a");
});

test("a document whose session was replaced sends nothing tagged before its reload, only session reads", async () => {
    await adoptSession(session("a"));
    await adoptSession(session("b", 2));
    answer(() => json(session("b", 2)));
    const refused = await api
        .get("/api/tenants/{tenant_id}/members/", { path: { tenant_id: 1 } })
        .catch((failure) => failure);
    expect(refused).toMatchObject({ code: "session_changed" });
    const signingOut = await authRequest("auth/session", "DELETE").catch(
        (failure) => failure,
    );
    expect(signingOut).toMatchObject({ code: "session_changed" });
    await loadSession();
    expect(sent()).toEqual(["GET /api/session/ null"]);
});

test("U5: a deletion answered after the session changed is not delivered to its caller", async () => {
    await adoptSession(session("a"));
    const { gate, release } = held();
    answer(async () => {
        await gate;
        return new Response(null, { status: 204 });
    });
    let delivered = false;
    const removal = api
        .delete("/api/tenants/{tenant_id}/members/{user_id}/", {
            path: { tenant_id: 1, user_id: 2 },
        })
        .then(() => {
            delivered = true;
        });
    await adoptSession(session("b", 2));
    release();
    const error = await removal.catch((failure) => failure);
    expect(error).toMatchObject({ code: "session_changed" });
    expect(delivered).toBe(false);
});

test("V1: a request that waited behind another one or for its retry is not sent once the document shows another session", async () => {
    await adoptSession(session("a"));
    const { gate, release } = held();
    const { arrived, arrive } = arrival();
    answer(
        async () => {
            arrive();
            await gate;
            return json({ status: 200, data: [] });
        },
        changed,
        () => json(session("b", 2)),
    );
    const read = authRequest("account/providers", "GET");
    const signingOut = authRequest("auth/session", "DELETE");
    await arrived;
    await adoptSession(session("b", 2));
    release();
    await read.catch(() => undefined);
    const error = await signingOut.catch((failure) => failure);
    expect(error).toMatchObject({ code: "session_changed" });
    expect(sent()).toEqual(["GET /api/auth/browser/v1/account/providers a"]);

    calls = [];
    startDocument();
    await adoptSession(session("c", 3));
    answer(
        () => json(errorBody("session_busy", "Busy.", { retry_after: 0 }), 429),
        changed,
        () => json(session("d", 4)),
    );
    const retried = api
        .post("/api/tenants/{tenant_id}/sites/", site)
        .catch((failure) => failure);
    await adoptSession(session("d", 4));
    expect(await retried).toMatchObject({ code: "session_changed" });
    expect(sent()).toEqual(["POST /api/tenants/1/sites/ c"]);
});

const signedOutAnswer = () =>
    json({ status: 401, meta: { is_authenticated: false } }, 401);
const notSent = {
    message: "This request was not sent because signing out was in progress.",
    status: 0,
    code: null,
};

test("a read waiting for its busy retry sends nothing after the sign-out was confirmed", async () => {
    await adoptSession(session("a"));
    const { gate, release } = held();
    const reply = async (call: Call) => {
        if (call.method === "DELETE") return signedOutAnswer();
        if (calls.length > 2)
            return json({ results: [], count: 0, next: null, previous: null });
        await gate;
        return json(
            errorBody("session_busy", "Busy.", { retry_after: 0 }),
            429,
        );
    };
    answer(reply, reply, reply);
    const read = api.get("/api/tenants/").catch((failure) => failure);
    await signOut();
    expect(sessionState().retired).toBe("signedOutHere");
    release();
    expect(await read).toMatchObject(notSent);
    expect(sent()).toEqual([
        "GET /api/tenants/ a",
        "DELETE /api/auth/browser/v1/auth/session a",
    ]);
});

for (const outcome of ["refused", "confirmed"] as const) {
    test(`a save waiting for its busy retry while this tab signs out is told it was not sent; the sign-out is ${outcome}`, async () => {
        await adoptSession(session("a"));
        const { gate, release } = held();
        const { arrived, arrive } = arrival();
        answer(
            () => {
                arrive();
                return json(
                    errorBody("session_busy", "Busy.", { retry_after: 0 }),
                    429,
                );
            },
            async () => {
                await gate;
                return outcome === "refused"
                    ? json(errorBody("csrf_failed", "The page expired."), 403)
                    : signedOutAnswer();
            },
        );
        const saving = api
            .post("/api/tenants/{tenant_id}/sites/", site)
            .catch((failure) => failure);
        await arrived;
        const ending = signOut().catch((failure) => failure);
        const refused = await saving;
        release();
        const ended = await ending;
        expect(refused).toBeInstanceOf(ApiError);
        expect(refused).toMatchObject(notSent);
        expect(answerLost(refused)).toBe(false);
        expect(sent()).toEqual([
            "POST /api/tenants/1/sites/ a",
            "DELETE /api/auth/browser/v1/auth/session a",
        ]);
        if (outcome === "refused") {
            expect(ended).toMatchObject({ status: 403, code: "csrf_failed" });
            expect(sessionState().retired).toBeUndefined();
            expect(sessionState()).toMatchObject({
                signOut: { status: "failed" },
            });
        } else {
            expect(ended).toBeUndefined();
            expect(sessionState().retired).toBe("signedOutHere");
        }
    });
}

test("a sign-out answered 200 with a signed-out body fails and keeps the document until allauth's signed-out answer", async () => {
    await adoptSession(session("a"));
    answer(
        () => json({ status: 401, meta: { is_authenticated: false } }),
        signedOutAnswer,
    );
    const failed = await signOut().catch((failure) => failure);
    expect(failed).toBeInstanceOf(ApiError);
    expect(failed).toMatchObject({
        message: "The server returned an invalid response. Please try again.",
    });
    expect(sessionState().retired).toBeUndefined();
    expect(sessionState()).toMatchObject({
        signOut: { status: "failed" },
    });
    expect(sent()).toEqual(["DELETE /api/auth/browser/v1/auth/session a"]);
    await signOut();
    expect(sessionState().retired).toBe("signedOutHere");
    expect(sent()).toEqual([
        "DELETE /api/auth/browser/v1/auth/session a",
        "DELETE /api/auth/browser/v1/auth/session a",
    ]);
});

test("a save whose connection failed may have been applied", async () => {
    await adoptSession(session("a"));
    answer(() => {
        throw new TypeError("Failed to fetch");
    });
    const lost = await api
        .post("/api/tenants/{tenant_id}/sites/", site)
        .catch((failure) => failure);
    expect(lost).toMatchObject({ status: 0, code: null });
    expect(answerLost(lost)).toBe(true);
});

test("a read begun during this tab's sign-out is refused as not sent before the sign-out is answered", async () => {
    await adoptSession(session("a"));
    const { gate, release } = held();
    const { arrived, arrive } = arrival();
    answer(
        async () => {
            arrive();
            await gate;
            return signedOutAnswer();
        },
        () => json({ status: 200, data: [] }),
    );
    const ending = signOut();
    const read = recorded(authRequest("account/providers", "GET"));
    await arrived;
    await drained();
    expect(read).toMatchObject({ settled: true, outcome: notSent });
    expect(sent()).toEqual(["DELETE /api/auth/browser/v1/auth/session a"]);
    release();
    await ending;
    expect(sent()).toEqual(["DELETE /api/auth/browser/v1/auth/session a"]);
});

test("a read refused after the document adopted another session during this tab's sign-out is told the session changed", async () => {
    await adoptSession(session("a"));
    const { gate, release } = held();
    const { arrived, arrive } = arrival();
    answer(changed, async () => {
        arrive();
        await gate;
        return json(session("b", 2));
    });
    let adopted: Promise<unknown> | undefined;
    Object.assign(globalThis.window.localStorage, {
        setItem: (key: string, value: string) => {
            stored.set(key, value);
            adopted ??= authRequest("account/providers", "GET").catch(
                (failure) => failure,
            );
        },
    });
    const ending = signOut().catch((failure) => failure);
    const first = recorded(authRequest("account/providers", "GET"));
    await arrived;
    const second = recorded(authRequest("account/providers", "GET"));
    await drained();
    expect(first).toMatchObject({ settled: true, outcome: notSent });
    expect(second).toMatchObject({ settled: true, outcome: notSent });
    expect(sent()).toEqual([
        "DELETE /api/auth/browser/v1/auth/session a",
        "GET /api/session/ null",
    ]);
    release();
    expect(await ending).toMatchObject({ code: "session_changed" });
    expect(await adopted).toMatchObject({ code: "session_changed" });
    expect(sent()).toEqual([
        "DELETE /api/auth/browser/v1/auth/session a",
        "GET /api/session/ null",
    ]);
});

test("a request prepared after this tab's confirmed sign-out is told it was not sent", async () => {
    await adoptSession(session("a"));
    answer(signedOutAnswer);
    await signOut();
    const saving = await api
        .post("/api/tenants/{tenant_id}/sites/", site)
        .catch((failure) => failure);
    const reading = await authRequest("account/providers", "GET").catch(
        (failure) => failure,
    );
    expect(saving).toMatchObject(notSent);
    expect(reading).toMatchObject(notSent);
    expect(sent()).toEqual(["DELETE /api/auth/browser/v1/auth/session a"]);
});

test("a document retired by its own sign-out keeps telling a waiting request it was not sent after reading another session", async () => {
    await adoptSession(session("a"));
    const { gate, release } = held();
    answer(
        async () => {
            await gate;
            return json(
                errorBody("session_busy", "Busy.", { retry_after: 0 }),
                429,
            );
        },
        signedOutAnswer,
        () => json(session("b", 2)),
    );
    const read = api.get("/api/tenants/").catch((failure) => failure);
    await signOut();
    expect((await loadSession()).session_tag).toBe("b");
    release();
    expect(await read).toMatchObject(notSent);
    expect(sent()).toEqual([
        "GET /api/tenants/ a",
        "DELETE /api/auth/browser/v1/auth/session a",
        "GET /api/session/ null",
    ]);
});

test("an answer that arrives after this tab's confirmed sign-out is withheld as a changed session, not as unsent", async () => {
    await adoptSession(session("a"));
    const { gate, release } = held();
    answer(async () => {
        await gate;
        return json({ results: [], count: 0, next: null, previous: null });
    }, signedOutAnswer);
    const read = api.get("/api/tenants/").catch((failure) => failure);
    await signOut();
    release();
    const withheld = await read;
    expect(withheld).toMatchObject({ code: "session_changed" });
    expect(withheld).not.toBeInstanceOf(NotSentError);
    expect(sent()).toEqual([
        "GET /api/tenants/ a",
        "DELETE /api/auth/browser/v1/auth/session a",
    ]);
});

test("V2: a sign-out is never sent a second time for another account", async () => {
    await adoptSession(session("a"));
    answer(changed, () => json(session("b", 2)));
    const error = await signOut().catch((failure) => failure);
    expect(error).toMatchObject({ code: "session_changed" });
    expect(sessionState().session?.user?.id).toBe(2);
    expect(sent()).toEqual([
        "DELETE /api/auth/browser/v1/auth/session a",
        "GET /api/session/ null",
    ]);
});

test("V3: tabs store only the tags their reads observed, so crossing reads converge", async () => {
    await adoptSession(session("a"));
    answer(
        () => json(session("b", 2)),
        () => json(session("a")),
    );
    await loadSession();
    expect(stored.get(observedTagKey)).toBe("b");
    await adoptSession(session("a"));
    expect(stored.get(observedTagKey)).toBe("a");
    expect([...stored.values()]).toEqual(["a"]);
});

test("V4: a refused sign-out changes no observed tag", async () => {
    await adoptSession(session("a"));
    answer(() => json(errorBody("csrf_failed", "The page expired."), 403));
    const error = await signOut().catch((failure) => failure);
    expect(error).toMatchObject({ status: 403, code: "csrf_failed" });
    expect(stored.get(observedTagKey)).toBe("a");
    expect(sessionState().session?.user?.id).toBe(1);
});

test("V5: the first session read of a new document announces the session it finds", async () => {
    stored.set(observedTagKey, "a");
    answer(() => json(signedOut));
    await loadSession();
    expect(stored.get(observedTagKey)).toBe("");
});

test("a read under a key another tab's confirmation replaced does not end this tab's session", async () => {
    await adoptSession(session("a"));
    answer(
        () => json(signedOut),
        () => json(session("a")),
    );
    await loadSession();
    expect(sessionState()).toMatchObject({
        ended: false,
        session: { user: { id: 1 }, session_tag: "a" },
    });
    expect(calls).toHaveLength(2);
});

test("a request refused for a renewed key of the same session is sent once more", async () => {
    await adoptSession(session("a"));
    answer(
        changed,
        () => json({ ...session("a"), csrf_token: "renewed" }),
        () => json({ results: [], count: 0, next: null, previous: null }),
    );
    const workspaces = await api.get("/api/tenants/");
    expect(workspaces).toMatchObject({ results: [] });
    expect(sent()).toEqual([
        "GET /api/tenants/ a",
        "GET /api/session/ null",
        "GET /api/tenants/ a",
    ]);
});

test("a request refused twice for a changed session is not sent a third time", async () => {
    await adoptSession(session("a"));
    answer(changed, () => json(session("a")), changed);
    const error = await api.get("/api/tenants/").catch((failure) => failure);
    expect(error).toMatchObject({ status: 409, code: "session_changed" });
    expect(calls).toHaveLength(3);
});

test("a read started before another tab changed the session is not reused after the change", async () => {
    await adoptSession(session("a"));
    const { gate, release } = held();
    answer(
        async () => {
            await gate;
            return json(session("a"));
        },
        () => json(signedOut),
        () => json(signedOut),
    );
    const earlier = loadSession();
    stored.set(observedTagKey, "");
    const later = loadSession();
    release();
    await Promise.all([earlier, later]);
    expect(calls).toHaveLength(3);
    expect(sessionState().session?.user).toBeNull();
    expect(stored.get(observedTagKey)).toBe("");
});

test("a read that settles after a newer one does not adopt its answer", async () => {
    await adoptSession(session("a"));
    const { gate, release } = held();
    const reads = [
        async () => {
            await gate;
            return json(signedOut);
        },
        () => json(session("b", 2)),
        () => json(session("a")),
    ];
    const reply = (call: Call) => {
        if (call.method === "POST") return json({ id: 7 }, 201);
        const next = reads.shift();
        if (!next) throw new Error("No session read is prepared.");
        return next();
    };
    answer(reply, reply, reply, reply);
    const earlier = loadSession();
    await api.post("/api/tenants/{tenant_id}/sites/", site);
    const later = loadSession();
    release();
    expect((await later).session_tag).toBe("b");
    expect((await earlier).session_tag).toBe("b");
    expect(reads).toHaveLength(0);
    expect(sessionState().session?.session_tag).toBe("b");
    expect(stored.get(observedTagKey)).toBe("b");
});

// Runs every promise reaction already queued; no time passes.
const drained = () => new Promise((resolve) => setImmediate(resolve));

function arrival() {
    let arrive!: () => void;
    const arrived = new Promise<void>((resolve) => {
        arrive = resolve;
    });
    return { arrived, arrive };
}

function recorded(promise: Promise<unknown>) {
    const record: { settled: boolean; outcome?: unknown } = { settled: false };
    const settle = (outcome: unknown) =>
        Object.assign(record, { settled: true, outcome });
    promise.then(settle, settle);
    return record;
}

test("a read that settles after another tab changed the session adopts the answer of a read sent after the change", async () => {
    await adoptSession(session("a"));
    const { gate, release } = held();
    answer(
        async () => {
            await gate;
            return json(session("a"));
        },
        () => json(signedOut),
        () => json(signedOut),
    );
    const earlier = loadSession();
    stored.set(observedTagKey, "");
    release();
    expect((await earlier).user).toBeNull();
    expect(calls).toHaveLength(3);
    expect(sessionState().session?.user).toBeNull();
    expect(stored.get(observedTagKey)).toBe("");
});

for (const later of ["answers", "fails"] as const) {
    test(`a read that settles while a read sent after another tab's change is in flight adopts nothing; the later read ${later}`, async () => {
        await adoptSession(session("a"));
        const first = held();
        const second = held();
        const { arrived, arrive } = arrival();
        answer(
            async () => {
                await first.gate;
                return json(session("a"));
            },
            async () => {
                arrive();
                await second.gate;
                return later === "answers"
                    ? json(signedOut)
                    : json(errorBody("server_error", "Unavailable."), 503);
            },
            () => json(signedOut),
        );
        const earlier = loadSession().catch((failure: unknown) => failure);
        stored.set(observedTagKey, "");
        const newer = loadSession().catch((failure: unknown) => failure);
        first.release();
        await arrived;
        await drained();
        expect(sessionState().session?.session_tag).toBe("a");
        expect(stored.get(observedTagKey)).toBe("");
        expect(isCurrent(sessionState().session)).toBe(false);
        second.release();
        const [settled, answered] = await Promise.all([earlier, newer]);
        expect(settled).toBe(answered);
        expect(stored.get(observedTagKey)).toBe("");
        if (later === "answers") {
            expect(sessionState().session?.user).toBeNull();
            expect(calls).toHaveLength(3);
        } else {
            expect(answered).toBeInstanceOf(ApiError);
            expect(sessionState().session?.session_tag).toBe("a");
            expect(isCurrent(sessionState().session)).toBe(false);
            expect(calls).toHaveLength(2);
        }
    });
}

test("a session adopted from a write's answer is kept over a read that started before the write", async () => {
    await adoptSession(session("a"));
    const first = held();
    const second = held();
    const turkish = { ...session("a"), language: "tr" };
    answer(
        async () => {
            await first.gate;
            return json(signedOut);
        },
        () => json(turkish),
        async () => {
            await second.gate;
            return json(session("a"));
        },
    );
    const earlier = loadSession();
    const language = api.post("/api/session/language/", {
        body: { language: "tr" },
    });
    first.release();
    await adoptSession(await language);
    second.release();
    expect((await earlier).language).toBe("tr");
    expect(sessionState().session?.language).toBe("tr");
    expect(sent()).toEqual([
        "GET /api/session/ null",
        "POST /api/session/language/ a",
        "GET /api/session/ null",
    ]);
});
