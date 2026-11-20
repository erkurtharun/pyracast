import { expect, test } from "@playwright/test";
import { api } from "../src/lib/api/client";
import { ApiError } from "../src/lib/api/errors";
import { keys } from "../src/lib/api/keys";
import { authRequest } from "../src/lib/auth-api";
import {
    adoptSession,
    isCurrent,
    loadSession,
    observedTagKey,
    sessionOwner,
    sessionState,
    signOut,
    type Session,
} from "../src/lib/session-client";
import { errorBody } from "./support/errors";

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
    adoptSession(signedOut);
});
test.afterEach(() => {
    globalThis.fetch = realFetch;
    Object.assign(globalThis, { window: realWindow });
});

test("U1: a save in flight while another session takes over is neither repeated nor applied", async () => {
    adoptSession(session("a"));
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
    const { gate, release } = held();
    answer(async () => {
        await gate;
        return json({ id: 7 }, 201);
    });
    const saving = api.post("/api/tenants/{tenant_id}/sites/", site);
    adoptSession(session("c", 3));
    release();
    const late = await saving.catch((failure) => failure);
    expect(late).toBeInstanceOf(ApiError);
    expect(late).toMatchObject({ code: "session_changed" });
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
    adoptSession(session("a"));
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

test("U5: a deletion answered after the session changed is not delivered to its caller", async () => {
    adoptSession(session("a"));
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
    adoptSession(session("b", 2));
    release();
    const error = await removal.catch((failure) => failure);
    expect(error).toMatchObject({ code: "session_changed" });
    expect(delivered).toBe(false);
});

test("V1: a request that waited behind another one is sent for the session it was prepared for", async () => {
    adoptSession(session("a"));
    const { gate, release } = held();
    answer(
        async () => {
            await gate;
            return json({ status: 200, data: [] });
        },
        changed,
        () => json(session("b", 2)),
    );
    const read = authRequest("account/providers", "GET");
    const signingOut = authRequest("auth/session", "DELETE");
    adoptSession(session("b", 2));
    release();
    await read.catch(() => undefined);
    const error = await signingOut.catch((failure) => failure);
    expect(error).toMatchObject({ code: "session_changed" });
    expect(sent()).toEqual([
        "GET /api/auth/browser/v1/account/providers a",
        "DELETE /api/auth/browser/v1/auth/session a",
        "GET /api/session/ null",
    ]);

    calls = [];
    adoptSession(session("c", 3));
    answer(
        () => json(errorBody("session_busy", "Busy.", { retry_after: 0 }), 429),
        changed,
        () => json(session("d", 4)),
    );
    const retried = api
        .post("/api/tenants/{tenant_id}/sites/", site)
        .catch((failure) => failure);
    adoptSession(session("d", 4));
    expect(await retried).toMatchObject({ code: "session_changed" });
    expect(sent().slice(0, 2)).toEqual([
        "POST /api/tenants/1/sites/ c",
        "POST /api/tenants/1/sites/ c",
    ]);
});

test("V2: a sign-out is never sent a second time for another account", async () => {
    adoptSession(session("a"));
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
    adoptSession(session("a"));
    answer(
        () => json(session("b", 2)),
        () => json(session("a")),
    );
    await loadSession();
    expect(stored.get(observedTagKey)).toBe("b");
    adoptSession(session("a"));
    expect(stored.get(observedTagKey)).toBe("a");
    expect([...stored.values()]).toEqual(["a"]);
});

test("V4: a refused sign-out changes no observed tag", async () => {
    adoptSession(session("a"));
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
    adoptSession(session("a"));
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
    adoptSession(session("a"));
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
    adoptSession(session("a"));
    answer(changed, () => json(session("a")), changed);
    const error = await api.get("/api/tenants/").catch((failure) => failure);
    expect(error).toMatchObject({ status: 409, code: "session_changed" });
    expect(calls).toHaveLength(3);
});
