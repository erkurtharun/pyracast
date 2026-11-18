import { expect, test } from "@playwright/test";
import { api, apiUrl } from "../src/lib/api/client";
import { ApiError } from "../src/lib/api/errors";
import { authRequest } from "../src/lib/auth-api";
import { loadSession, sessionState } from "../src/lib/session-client";
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
        calls.push(call);
        const next = answers.length > 1 ? answers.shift()! : answers[0]!;
        return next(call);
    };
}

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
    language: "en",
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
    const created = await api.post("/api/tenants/{tenant_id}/sites/", {
        path: { tenant_id: 1 },
        body: {
            name: "Plant",
            latitude: 36.9,
            longitude: 30.7,
            timezone: "Europe/Istanbul",
            dc_capacity_mw: 5,
            mounting_type: "fixed",
        },
    });
    expect(created).toEqual({ id: 7 });
    expect(calls.map((call) => `${call.method} ${call.url}`)).toEqual([
        "POST /api/tenants/1/sites/",
        "POST /api/tenants/1/sites/",
    ]);
    expect(calls[1]!.body).toBe(calls[0]!.body);
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

test("a second busy answer is reported instead of retried again", async () => {
    answer(() =>
        json(errorBody("session_busy", "Busy.", { retry_after: 0 }), 429),
    );
    const error = await api.get("/api/tenants/").catch((failure) => failure);
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
});
