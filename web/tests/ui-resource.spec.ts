import { expect, test } from "@playwright/test";
import { resourceState } from "../src/ui/resource-state";

test("a resource without data is loading until it has an error", () => {
    expect(resourceState({})).toEqual({ status: "loading" });
    expect(resourceState({ error: "Service is busy." })).toEqual({
        status: "failed",
        error: "Service is busy.",
    });
});

test("a resource with data stays ready when its refresh fails", () => {
    const data = { rows: [1] };
    expect(resourceState({ data })).toEqual({ status: "ready", data });
    expect(resourceState({ data, error: "Service is busy." })).toEqual({
        status: "ready",
        data,
    });
});

test("falsy data still counts as loaded", () => {
    expect(resourceState({ data: 0 })).toEqual({ status: "ready", data: 0 });
    expect(resourceState({ data: null, error: "Gone." })).toEqual({
        status: "ready",
        data: null,
    });
});
