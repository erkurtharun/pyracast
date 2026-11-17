import { expect, test } from "@playwright/test";
import { ApiError, errorFromResponse } from "../src/lib/api/errors";
import { authRequest } from "../src/lib/auth-api";
import { errorBody } from "./support/errors";

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
        errorBody(
            "server_error",
            "The server could not complete this request.",
            {
                request_id: "0123456789abcdef0123456789abcdef",
            },
        ),
    );
    expect(failure.requestId).toBe("0123456789abcdef0123456789abcdef");
    expect(failure.message).toBe(
        "The server could not complete this request. Reference: 0123456789abcdef0123456789abcdef",
    );
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
        globalThis.fetch = async () =>
            new Response(JSON.stringify(body), {
                status,
                headers: { "Content-Type": "application/json" },
            });
    }

    test("a contract body keeps every property", async () => {
        answer(
            429,
            errorBody("session_busy", "Another request is running.", {
                retry_after: 1,
                request_id: "0123456789abcdef0123456789abcdef",
            }),
        );
        const error = await authRequest("auth/session", "GET", "token").catch(
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

    test("an allauth body keeps its messages per input", async () => {
        answer(400, {
            status: 400,
            errors: [
                {
                    message: "Enter an email.",
                    param: "email",
                    code: "required",
                },
                { message: "Try again later.", code: "too_many" },
            ],
        });
        const error = await authRequest(
            "auth/login",
            "POST",
            "token",
            {},
        ).catch((failure: unknown) => failure);
        expect(error).toMatchObject({
            status: 400,
            code: null,
            message: "Enter an email. Try again later.",
            fields: { email: "Enter an email." },
        });
    });

    test("an allauth body without messages names the status", async () => {
        answer(409, { status: 409 });
        const error = await authRequest(
            "auth/provider/signup",
            "POST",
            "token",
            {},
        ).catch((failure: unknown) => failure);
        expect(error).toMatchObject({
            status: 409,
            message: "Request failed (409). Please try again.",
        });
    });
});
