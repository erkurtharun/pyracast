import { expect, test } from "@playwright/test";
import { ApiError, errorFromResponse } from "../src/lib/api/errors";
import { authRequest } from "../src/lib/auth-api";
import { allauthErrorBody, errorBody } from "./support/errors";

test("allauth fixtures join every message for the same field", () => {
    expect(
        allauthErrorBody(400, "invalid_request", [
            { param: "email", message: "Enter an email." },
            { param: "email", message: "Use your invited address." },
            { param: "password", message: "Enter a password." },
            { message: "Check your input." },
        ]),
    ).toEqual({
        status: 400,
        code: "invalid_request",
        message: "Enter an email.",
        fields: {
            email: "Enter an email. Use your invited address.",
            password: "Enter a password.",
        },
        errors: [
            { param: "email", message: "Enter an email." },
            { param: "email", message: "Use your invited address." },
            { param: "password", message: "Enter a password." },
            { message: "Check your input." },
        ],
    });
});

test("a contract body becomes one typed error", () => {
    const error = errorFromResponse(
        400,
        errorBody(
            "incident_capacity_exceeded",
            "Available capacity is too high.",
            {
                fields: { available_dc_mw: "Available capacity is too high." },
            },
        ),
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe("Available capacity is too high.");
    expect(error.status).toBe(400);
    expect(error.code).toBe("incident_capacity_exceeded");
    expect(error.fields).toEqual({
        available_dc_mw: "Available capacity is too high.",
    });
    expect(error.retryAfter).toBeNull();
    expect(error.requestId).toBeNull();
});

test("retry hints and request references are kept", () => {
    const busy = errorFromResponse(
        503,
        errorBody("busy", "The workspace is busy.", { retry_after: 1 }),
    );
    expect(busy.retryAfter).toBe(1);
    expect(busy.fields).toEqual({});
    const failure = errorFromResponse(
        500,
        errorBody("server_error", "Something went wrong. Try again.", {
            request_id: "0123456789abcdef0123456789abcdef",
        }),
    );
    expect(failure.requestId).toBe("0123456789abcdef0123456789abcdef");
    expect(failure.message).toBe(
        "Something went wrong. Try again. Reference: 0123456789abcdef0123456789abcdef",
    );
});

test("the gateway's maintenance notice is a coded error with its retry delay", () => {
    const error = errorFromResponse(
        503,
        errorBody(
            "maintenance",
            "Pyracast is being updated. Try again in a minute; your data is safe.",
            { retry_after: 60 },
        ),
    );
    expect(error.code).toBe("maintenance");
    expect(error.message).toBe(
        "Pyracast is being updated. Try again in a minute; your data is safe.",
    );
    expect(error.retryAfter).toBe(60);
    expect(error.status).toBe(503);
});

for (const [name, body] of [
    ["an empty body", null],
    ["a gateway page", "<html>Bad gateway</html>"],
    ["a list", ["invalid"]],
    ["a body without a message", { code: "busy" }],
    ["a body without a code", { message: "Unexpected" }],
] as const) {
    test(`${name} becomes a generic failure with the status`, () => {
        const error = errorFromResponse(502, body);
        expect(error.message).toBe("Request failed (502). Please try again.");
        expect(error.status).toBe(502);
        expect(error.code).toBeNull();
        expect(error.fields).toEqual({});
    });
}

test("client-side errors carry no server code", () => {
    const error = new ApiError("Confirm your identity first.");
    expect(error.status).toBe(0);
    expect(error.code).toBeNull();
    expect(error.fields).toEqual({});
});

test("malformed optional properties never reach typed fields", () => {
    const error = errorFromResponse(400, {
        code: "code_from_a_newer_server",
        message: "Check the values.",
        fields: { email: { invalid: true }, name: "Too long." },
        retry_after: "1",
        request_id: { id: 1 },
    });
    expect(error.message).toBe("Check the values.");
    expect(error.code).toBeNull();
    expect(error.fields).toEqual({ name: "Too long." });
    expect(error.retryAfter).toBeNull();
    expect(error.requestId).toBeNull();
    for (const fields of [["name"], "name", null]) {
        expect(
            errorFromResponse(400, { code: "busy", message: "Busy.", fields })
                .fields,
        ).toEqual({});
    }
    expect(
        errorFromResponse(503, {
            code: "busy",
            message: "Busy.",
            retry_after: -1,
        }).retryAfter,
    ).toBeNull();
});

test.describe("authentication requests", () => {
    const realFetch = globalThis.fetch;
    test.afterEach(() => {
        globalThis.fetch = realFetch;
    });

    function answer(status: number, body: unknown) {
        globalThis.fetch = () =>
            Promise.resolve(
                new Response(JSON.stringify(body), {
                    status,
                    headers: { "Content-Type": "application/json" },
                }),
            );
    }

    test("a contract body keeps every property", async () => {
        answer(
            429,
            errorBody("session_busy", "Another request is running.", {
                retry_after: 1,
                request_id: "0123456789abcdef0123456789abcdef",
            }),
        );
        const error = await authRequest("auth/session", "GET").catch(
            (failure: unknown) => failure,
        );
        expect(error).toBeInstanceOf(ApiError);
        expect(error).toMatchObject({
            status: 429,
            code: "session_busy",
            retryAfter: 1,
            requestId: "0123456789abcdef0123456789abcdef",
        });
    });

    const allauthErrors = [
        { message: "Enter an email.", param: "email", code: "required" },
        { message: "Try again later.", code: "too_many" },
    ];

    test("an allauth failure maps through its contract keys", async () => {
        answer(400, {
            status: 400,
            errors: allauthErrors,
            ...errorBody("invalid_request", "Enter an email.", {
                fields: { email: "Enter an email." },
            }),
        });
        const error = await authRequest("auth/login", "POST", {}).catch(
            (failure: unknown) => failure,
        );
        expect(error).toBeInstanceOf(ApiError);
        expect(error).toMatchObject({
            status: 400,
            code: "invalid_request",
            message: "Enter an email.",
            fields: { email: "Enter an email." },
        });
    });

    test("an allauth conflict carries its code", async () => {
        answer(409, {
            status: 409,
            ...errorBody("conflict", "The request conflicts."),
        });
        const error = await authRequest(
            "auth/provider/signup",
            "POST",
            {},
        ).catch((failure: unknown) => failure);
        expect(error).toMatchObject({
            status: 409,
            code: "conflict",
            message: "The request conflicts.",
        });
    });

    test("a signed-out state answer keeps its flow data", async () => {
        answer(401, {
            status: 401,
            data: { flows: [{ id: "login" }] },
            meta: { is_authenticated: false },
            ...errorBody("not_authenticated", "Sign in to continue."),
        });
        const result = await authRequest("auth/session", "GET");
        expect(result.data?.flows).toEqual([{ id: "login" }]);
        expect(result.meta?.is_authenticated).toBe(false);
    });

    test("a body without the contract keys names the status", async () => {
        answer(400, { status: 400, errors: allauthErrors });
        const error = await authRequest("auth/login", "POST", {}).catch(
            (failure: unknown) => failure,
        );
        expect(error).toMatchObject({
            status: 400,
            code: null,
            message: "Request failed (400). Please try again.",
            fields: {},
        });
    });
});
