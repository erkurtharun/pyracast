import { expect, test } from "@playwright/test";
import {
    api,
    apiUrl,
    buildUrl,
    type RequestOptions,
} from "../src/lib/api/client";
import { ApiError } from "../src/lib/api/errors";
import { authRequest } from "../src/lib/auth-api";
import {
    adoptSession,
    loadSession,
    sessionState,
    signOut,
} from "../src/lib/session-client";
import { errorBody } from "./support/errors";

type Call = {
    method: string;
    url: string;
    token: string | null;
    body?: string;
};
type Answer = Response | Promise<Response>;

const realFetch = globalThis.fetch;
let calls: Call[] = [];

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
            url: String(input),
            token: new Headers(init?.headers).get("X-CSRFToken"),
            body: typeof init?.body === "string" ? init.body : undefined,
        };
        const next = answers[calls.length];
        calls.push(call);
        if (!next) throw new Error("No answer is prepared for this request.");
        return next(call);
    };
}

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

const signedIn = (token: string) => ({
    user: {
        id: 1,
        username: "member",
        email: "member@example.test",
        has_usable_password: true,
        can_admin: false,
        mfa_required: false,
        language_preference: null,
    },
    csrf_token: token,
    language: "en" as const,
    expires_at: "2026-11-18T22:00:00Z",
});

test.beforeEach(() => {
    calls = [];
});
test.afterEach(() => {
    globalThis.fetch = realFetch;
});

test("a busy session is retried once after its Retry-After", async () => {
    answer(
        () => json(errorBody("session_busy", "Busy.", { retry_after: 0 }), 429),
        () => json({ id: 7 }, 201),
    );
    const created = await api.post("/api/tenants/{tenant_id}/sites/", site);
    expect(created).toEqual({ id: 7 });
    expect(calls.map((call) => `${call.method} ${call.url}`)).toEqual([
        "POST /api/tenants/1/sites/",
        "POST /api/tenants/1/sites/",
    ]);
    const [first, retry] = calls.map((call) => call.body);
    expect(first).toBe(JSON.stringify(site.body));
    expect(retry).toBe(first);
});

test("the retry waits for the announced Retry-After", async () => {
    answer(
        () => json(errorBody("session_busy", "Busy.", { retry_after: 1 }), 429),
        () => json({ results: [], count: 0, next: null, previous: null }),
    );
    const started = Date.now();
    await api.get("/api/tenants/");
    expect(Date.now() - started).toBeGreaterThanOrEqual(900);
    expect(calls).toHaveLength(2);
});

test("a change refused twice as busy is reported instead of sent a third time", async () => {
    const refused = () =>
        json(errorBody("session_busy", "Busy.", { retry_after: 0 }), 429);
    answer(refused, refused);
    const error = await api
        .post("/api/tenants/{tenant_id}/sites/", site)
        .catch((failure) => failure);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 429, code: "session_busy" });
    expect(calls).toHaveLength(2);
});

test("a busy workspace never replays a non-idempotent POST", async () => {
    answer(() => json(errorBody("busy", "Busy.", { retry_after: 0 }), 503));
    const error = await api
        .post("/api/tenants/{tenant_id}/regions/{region_id}/restore/", {
            path: { tenant_id: 1, region_id: 11 },
        })
        .catch((failure) => failure);
    expect(error).toMatchObject({ status: 503, code: "busy" });
    expect(calls).toHaveLength(1);
});

test("a failed connection reports an unconfirmed change once", async () => {
    answer(() => {
        throw new TypeError("Failed to fetch");
    });
    const error = await api
        .post("/api/tenants/{tenant_id}/regions/{region_id}/restore/", {
            path: { tenant_id: 1, region_id: 11 },
        })
        .catch((failure) => failure);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe(
        "The server did not confirm the change. Reload to check whether it was saved.",
    );
    expect(calls).toHaveLength(1);
});

test("a read without an answer is localized", async () => {
    answer(() => {
        throw new DOMException("The operation timed out.", "TimeoutError");
    });
    const error = await api.get("/api/tenants/").catch((failure) => failure);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe(
        "Could not reach the server. Check your connection and try again.",
    );
    expect(calls).toHaveLength(1);
});

test("a successful answer that is not JSON is refused", async () => {
    answer(
        () =>
            new Response("<html>Maintenance</html>", {
                status: 200,
                headers: { "Content-Type": "text/html" },
            }),
    );
    const error = await api.get("/api/tenants/").catch((failure) => failure);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
        status: 200,
        message: "The server returned an invalid response. Please try again.",
    });
});

test("unsafe requests carry the CSRF token of the current session", async () => {
    answer(
        () => json(signedIn("current-token")),
        () => json({ results: [], count: 0, next: null, previous: null }),
        () => new Response(null, { status: 204 }),
    );
    await loadSession();
    await api.get("/api/tenants/");
    await api.delete("/api/tenants/{tenant_id}/members/{user_id}/", {
        path: { tenant_id: 1, user_id: 2 },
    });
    expect(calls.map((call) => call.token)).toEqual([
        null,
        null,
        "current-token",
    ]);
});

test("a session read started before sign-out is not handed out after it", async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
        release = resolve;
    });
    answer(
        async () => {
            await held;
            return json(signedIn("before"));
        },
        () => json({ status: 401, meta: { is_authenticated: false } }, 401),
        () => json({ ...signedIn("after"), user: null, expires_at: null }),
    );
    const earlier = loadSession();
    const signOut = authRequest("auth/session", "DELETE");
    const later = loadSession();
    release();
    expect((await earlier).csrf_token).toBe("before");
    expect((await signOut).status).toBe(401);
    expect((await later).csrf_token).toBe("after");
    expect(calls.map((call) => `${call.method} ${call.url}`)).toEqual([
        "GET /api/session/",
        "DELETE /api/auth/browser/v1/auth/session",
        "GET /api/session/",
    ]);
    expect(sessionState().session?.user).toBeNull();
});

test("a not-authenticated answer ends the session once", async () => {
    answer(
        () => json(signedIn("token")),
        () => json(errorBody("not_authenticated", "Sign in."), 403),
        () => json(errorBody("not_authenticated", "Sign in."), 403),
        () => json({ ...signedIn("anonymous"), user: null, expires_at: null }),
    );
    await loadSession();
    expect(sessionState().ended).toBe(false);
    const failures = await Promise.all([
        api.get("/api/tenants/").catch((failure) => failure),
        api.get("/api/tenants/").catch((failure) => failure),
    ]);
    expect(failures.map((failure) => failure.code)).toEqual([
        "not_authenticated",
        "not_authenticated",
    ]);
    await expect.poll(() => sessionState().session?.user).toBeNull();
    expect(sessionState().ended).toBe(true);
    expect(calls.filter((call) => call.url === "/api/session/")).toHaveLength(
        2,
    );
});

const emptyList = { results: [], count: 0, next: null, previous: null };
const reauthenticated = { status: 200, meta: { is_authenticated: true } };

test("a not-authenticated answer arriving after a reauthentication is retried once the session read shows the session", async () => {
    answer(
        () => json(signedIn("token")),
        () => json(reauthenticated),
        () => json(errorBody("not_authenticated", "Sign in."), 401),
        () => json(signedIn("rotated")),
        () => json(emptyList),
    );
    await loadSession();
    await authRequest("auth/reauthenticate", "POST", { password: "secret" });
    const workspaces = await api.get("/api/tenants/");
    expect(workspaces).toEqual(emptyList);
    expect(sessionState()).toMatchObject({
        ended: false,
        session: { csrf_token: "rotated" },
    });
    expect(calls.map((call) => `${call.method} ${call.url}`)).toEqual([
        "GET /api/session/",
        "POST /api/auth/browser/v1/auth/reauthenticate",
        "GET /api/tenants/",
        "GET /api/session/",
        "GET /api/tenants/",
    ]);
});

test("a not-authenticated answer arriving before a reauthentication's answer waits for a session read sent after it", async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
        release = resolve;
    });
    let reauthenticationAnswered = false;
    let readAfterReauthentication = false;
    answer(
        () => json(signedIn("token")),
        async () => {
            await held;
            reauthenticationAnswered = true;
            return json(reauthenticated);
        },
        () => json(errorBody("not_authenticated", "Sign in."), 401),
        () => {
            readAfterReauthentication = reauthenticationAnswered;
            return json(signedIn("rotated"));
        },
        () => json(emptyList),
    );
    await loadSession();
    const reauthentication = authRequest("auth/reauthenticate", "POST", {
        password: "secret",
    });
    await expect.poll(() => calls.length).toBe(2);
    const workspaces = api.get("/api/tenants/");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(calls).toHaveLength(3);
    expect(sessionState()).toMatchObject({
        ended: false,
        session: { csrf_token: "token" },
    });
    release();
    expect((await reauthentication).status).toBe(200);
    expect(await workspaces).toEqual(emptyList);
    expect(readAfterReauthentication).toBe(true);
    expect(sessionState()).toMatchObject({
        ended: false,
        session: { csrf_token: "rotated" },
    });
    expect(calls.map((call) => `${call.method} ${call.url}`)).toEqual([
        "GET /api/session/",
        "POST /api/auth/browser/v1/auth/reauthenticate",
        "GET /api/tenants/",
        "GET /api/session/",
        "GET /api/tenants/",
    ]);
});

test("a repeated request refused again ends the session after another session read", async () => {
    answer(
        () => json(signedIn("token")),
        () => json(errorBody("not_authenticated", "Sign in."), 401),
        () => json(signedIn("rotated")),
        () => json(errorBody("not_authenticated", "Sign in."), 401),
        () => json({ ...signedIn("anonymous"), user: null, expires_at: null }),
    );
    await loadSession();
    const error = await api.get("/api/tenants/").catch((failure) => failure);
    expect(error).toMatchObject({ status: 401, code: "not_authenticated" });
    expect(sessionState()).toMatchObject({
        ended: true,
        session: { user: null },
    });
    expect(calls.map((call) => `${call.method} ${call.url}`)).toEqual([
        "GET /api/session/",
        "GET /api/tenants/",
        "GET /api/session/",
        "GET /api/tenants/",
        "GET /api/session/",
    ]);
});

test("an allauth flow answer does not end the session", async () => {
    answer(
        () => json(signedIn("token")),
        () =>
            json(
                {
                    status: 401,
                    meta: { is_authenticated: true },
                    ...errorBody("not_authenticated", "Confirm."),
                },
                401,
            ),
    );
    await loadSession();
    const result = await authRequest("account/email", "POST", {
        email: "new@example.test",
    });
    expect(result.status).toBe(401);
    expect(sessionState().ended).toBe(false);
    expect(calls).toHaveLength(2);
});

test("path parameters and query values build one encoded URL", () => {
    expect(
        apiUrl("/api/tenants/{tenant_id}/change-requests/", {
            path: { tenant_id: 1 },
            query: { scope: "pending", page: 2, plant_id: undefined },
        }),
    ).toBe("/api/tenants/1/change-requests/?scope=pending&page=2");
    expect(
        apiUrl(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/forecast/",
            {
                path: { tenant_id: 1, region_id: 11, plant_id: 21 },
                query: { date: "2026-11-18", data_source: "auto" },
            },
        ),
    ).toBe(
        "/api/tenants/1/regions/11/plants/21/forecast/?date=2026-11-18&data_source=auto",
    );
    expect(
        apiUrl(
            "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/forecast/",
            {
                path: { tenant_id: 1, region_id: 11, plant_id: 21 },
                query: {
                    date: "2026-11-18",
                    as_of: "2026-11-18T10:00:00+03:00",
                },
            },
        ),
    ).toBe(
        "/api/tenants/1/regions/11/plants/21/forecast/?date=2026-11-18&as_of=2026-11-18T10%3A00%3A00%2B03%3A00",
    );
});

test("bodyless operations accept no body in their typed options", () => {
    const getBodyIsForbidden: NonNullable<
        RequestOptions<"/api/tenants/", "get">["body"]
    > extends never
        ? true
        : false = true;
    const deleteBodyIsForbidden: NonNullable<
        RequestOptions<
            "/api/tenants/{tenant_id}/members/{user_id}/",
            "delete"
        >["body"]
    > extends never
        ? true
        : false = true;
    expect(getBodyIsForbidden).toBe(true);
    expect(deleteBodyIsForbidden).toBe(true);
});

test("a URL requires every named path parameter", () => {
    expect(() => buildUrl("/api/tenants/{tenant_id}/", {})).toThrow(
        "Missing path parameter: tenant_id",
    );
});

test("a session read that discovers expiry records the signed-out transition", async () => {
    adoptSession(signedIn("token"));
    answer(() =>
        json({ ...signedIn("anonymous"), user: null, expires_at: null }),
    );
    await loadSession();
    expect(sessionState().session?.user).toBeNull();
    expect(sessionState().ended).toBe(true);
});

test("an explicit sign-out returns to sign-in without an expiry notice", async () => {
    adoptSession(signedIn("token"));
    answer(
        () => json({ status: 401, meta: { is_authenticated: false } }, 401),
        () => json({ ...signedIn("anonymous"), user: null, expires_at: null }),
    );
    await signOut();
    expect(sessionState().session?.user).toBeNull();
    expect(sessionState().ended).toBe(false);
});

const signedOutAnswer = () =>
    json({ status: 401, meta: { is_authenticated: false } }, 401);

test("a sign-out answered for a key another tab replaced signs out the replacing session", async () => {
    adoptSession(signedIn("token"));
    answer(
        signedOutAnswer,
        () => json(signedIn("rotated")),
        signedOutAnswer,
        () => json({ ...signedIn("anonymous"), user: null, expires_at: null }),
    );
    const session = await signOut();
    expect(session.user).toBeNull();
    expect(sessionState()).toMatchObject({
        ended: false,
        session: { user: null },
    });
    expect(
        calls.map((call) => `${call.method} ${call.url} ${call.token}`),
    ).toEqual([
        "DELETE /api/auth/browser/v1/auth/session token",
        "GET /api/session/ null",
        "DELETE /api/auth/browser/v1/auth/session rotated",
        "GET /api/session/ null",
    ]);
});

test("an account read answered as signed out is repeated once the session read shows the session", async () => {
    answer(
        () => json(signedIn("token")),
        signedOutAnswer,
        () => json(signedIn("rotated")),
        () => json({ status: 200, data: [] }),
    );
    await loadSession();
    const result = await authRequest("account/authenticators", "GET");
    expect(result).toEqual({ status: 200, data: [] });
    expect(sessionState()).toMatchObject({
        ended: false,
        session: { csrf_token: "rotated" },
    });
    expect(calls.map((call) => `${call.method} ${call.url}`)).toEqual([
        "GET /api/session/",
        "GET /api/auth/browser/v1/account/authenticators",
        "GET /api/session/",
        "GET /api/auth/browser/v1/account/authenticators",
    ]);
});

test("an account read answered as signed out is not repeated when the session read agrees", async () => {
    answer(
        () => json(signedIn("token")),
        signedOutAnswer,
        () => json({ ...signedIn("anonymous"), user: null, expires_at: null }),
    );
    await loadSession();
    const result = await authRequest("account/authenticators", "GET");
    expect(result.status).toBe(401);
    expect(sessionState()).toMatchObject({
        ended: true,
        session: { user: null },
    });
    expect(calls).toHaveLength(3);
});

test("an account change answered as signed out is not repeated", async () => {
    answer(() => json(signedIn("token")), signedOutAnswer);
    await loadSession();
    const result = await authRequest("account/authenticators/totp", "POST", {
        code: "123456",
    });
    expect(result.status).toBe(401);
    expect(calls).toHaveLength(2);
});

test("a sign-out that still finds a signed-in session after repeating it is reported", async () => {
    adoptSession(signedIn("token"));
    answer(
        signedOutAnswer,
        () => json(signedIn("rotated")),
        signedOutAnswer,
        () => json(signedIn("again")),
    );
    const error = await signOut().catch((failure) => failure);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe("You are still signed in. Sign out again.");
    expect(sessionState()).toMatchObject({
        ended: false,
        session: { csrf_token: "again" },
    });
    expect(calls).toHaveLength(4);
});

const otherAccount = (token: string) => ({
    ...signedIn(token),
    user: {
        ...signedIn(token).user,
        id: 2,
        username: "other",
        email: "other@example.test",
    },
});

test("a save refused as signed out is not sent again while another account's session is confirmed", async () => {
    answer(
        () => json(signedIn("token")),
        () => json(errorBody("not_authenticated", "Sign in."), 401),
        () => json(otherAccount("other-token")),
    );
    await loadSession();
    const error = await api
        .post("/api/tenants/{tenant_id}/sites/", site)
        .catch((failure) => failure);
    expect(error).toMatchObject({ status: 401, code: "not_authenticated" });
    expect(sessionState().session?.user?.id).toBe(2);
    expect(
        calls.map((call) => `${call.method} ${call.url} ${call.token}`),
    ).toEqual([
        "GET /api/session/ null",
        "POST /api/tenants/1/sites/ token",
        "GET /api/session/ null",
    ]);
});

test("an account read answered as signed out is not repeated while another account's session is confirmed", async () => {
    answer(
        () => json(signedIn("token")),
        signedOutAnswer,
        () => json(otherAccount("other-token")),
    );
    await loadSession();
    const result = await authRequest("account/authenticators", "GET");
    expect(result.status).toBe(401);
    expect(sessionState().session?.user?.id).toBe(2);
    expect(calls).toHaveLength(3);
});

test("a sign-out is not sent a second time under another account", async () => {
    adoptSession(signedIn("token"));
    answer(signedOutAnswer, () => json(otherAccount("other-token")));
    const error = await signOut().catch((failure) => failure);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe("You are still signed in. Sign out again.");
    expect(sessionState()).toMatchObject({
        ended: false,
        session: { user: { id: 2 }, csrf_token: "other-token" },
    });
    expect(
        calls.map((call) => `${call.method} ${call.url} ${call.token}`),
    ).toEqual([
        "DELETE /api/auth/browser/v1/auth/session token",
        "GET /api/session/ null",
    ]);
});
