import { expect, test } from "@playwright/test";
import { ApiError, errorFromResponse } from "../src/lib/api/errors";
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
