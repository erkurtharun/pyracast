import { expect, test } from "@playwright/test";
import {
    binnedAt,
    canonicalCoordinate,
    coordinatePair,
    coordinatePattern,
    coordinateValue,
    type CoordinateAxis,
    sameLocation,
    supportedZones,
    zoneChoices,
} from "../src/features/portfolio/site-location";
import {
    onboardingSteps,
    refusalTarget,
    unplacedFields,
} from "../src/features/portfolio/onboarding-steps";
import { matchesSearch } from "../src/features/portfolio/portfolio-search";
import { coordinatePastes } from "./support/coordinate-pastes";
import coordinateRows from "./support/coordinate-values.json";

// The console's form is held to the same table (tests/test_admin_plant_console.py). Spaces around
// a value are ignored on both sides: a value copied from a sheet often carries one.
const coordinateValues = coordinateRows.map((row) => ({
    ...row,
    axis: coordinateAxis(row.axis),
}));

function coordinateAxis(name: string): CoordinateAxis {
    if (name === "latitude" || name === "longitude") return name;
    throw new Error(`${name} is no coordinate axis`);
}

test("a pasted text fills both coordinates only when it is a pair of coordinates in range", () => {
    for (const paste of coordinatePastes)
        expect(coordinatePair(paste.text), paste.text).toEqual(
            "pair" in paste ? paste.pair : null,
        );
});

test("a pasted text that is no pair is one latitude or refused by the latitude field", () => {
    for (const paste of coordinatePastes)
        if ("single" in paste)
            expect(
                Number.isNaN(coordinateValue("latitude", paste.text)),
                paste.text,
            ).toBe(paste.single === "refused");
});

test("a single coordinate with one decimal comma or point is that number", () => {
    for (const { axis, text, value } of coordinateValues)
        if (value !== undefined)
            expect(coordinateValue(axis, text), text).toBe(value);
});

test("a coordinate that is not one plain number in range is refused", () => {
    for (const { axis, text, reason } of coordinateValues)
        if (reason !== undefined)
            expect(coordinateValue(axis, text), text).toBeNaN();
});

test("the coordinate field's pattern accepts exactly the values the rule accepts", () => {
    for (const { axis, text } of coordinateValues) {
        // Browsers compile a pattern attribute whole, with the v flag.
        const field = new RegExp(`^(?:${coordinatePattern(axis)})$`, "v");
        expect(field.test(text), text).toBe(
            !Number.isNaN(coordinateValue(axis, text)),
        );
    }
});

test("the zone list adds UTC and the current zone to the browser's list, once and sorted", () => {
    expect(
        zoneChoices(["Europe/Istanbul", "Asia/Tokyo"], "Asia/Calcutta"),
    ).toEqual(["Asia/Calcutta", "Asia/Tokyo", "Europe/Istanbul", "UTC"]);
    expect(zoneChoices(["UTC", "Europe/Istanbul"], "UTC")).toEqual([
        "Europe/Istanbul",
        "UTC",
    ]);
    expect(zoneChoices(["Europe/Istanbul"], "")).toEqual([
        "Europe/Istanbul",
        "UTC",
    ]);
});

test("a browser without a zone list gets none, so the form asks for the zone as text", () => {
    expect(supportedZones({})).toBeNull();
    expect(
        supportedZones({ supportedValuesOf: () => ["Europe/Istanbul"] }),
    ).toEqual(["Europe/Istanbul"]);
});

test("coordinates are rounded as the server stores them, half away from zero at six decimals", () => {
    expect(canonicalCoordinate(40.0000005)).toBe(40.000001);
    expect(canonicalCoordinate(-40.0000005)).toBe(-40.000001);
    expect(canonicalCoordinate(40.0000004999)).toBe(40);
    expect(canonicalCoordinate(36.1234567)).toBe(36.123457);
    expect(canonicalCoordinate(1e-7)).toBe(0);
    expect(canonicalCoordinate(5e-7)).toBe(0.000001);
    expect(canonicalCoordinate(-180)).toBe(-180);
});

test("a location matches a stored one when both round to the same stored value", () => {
    const stored = { latitude: 36.123457, longitude: 30.7 };
    expect(
        sameLocation(stored, { latitude: 36.1234567, longitude: 30.7 }),
    ).toBe(true);
    expect(sameLocation(stored, { latitude: 36.12346, longitude: 30.7 })).toBe(
        false,
    );
    expect(
        sameLocation(stored, { latitude: 36.123457, longitude: 30.71 }),
    ).toBe(false);
    expect(
        sameLocation(
            { latitude: 40.000001, longitude: 30 },
            { latitude: 40.0000005, longitude: 30 },
        ),
    ).toBe(true);
    expect(
        sameLocation(
            { latitude: 40, longitude: 30 },
            { latitude: 40.0000005, longitude: 30 },
        ),
    ).toBe(false);
});

test("the bin offers the most recently deleted restorable plant at the place and counts the others", () => {
    const row = (id: number, deleted_at: string, blocked = false) => ({
        id,
        latitude: 37,
        longitude: 31,
        deleted_at,
        restore_blocker: blocked ? ("trash_retention_expired" as const) : null,
    });
    const older = row(1, "2026-11-01T00:00:00Z");
    const newer = row(2, "2026-11-20T00:00:00Z");
    const expired = row(3, "2026-11-21T00:00:00Z", true);
    const elsewhere = { ...row(4, "2026-11-21T00:00:00Z"), latitude: 38 };
    const place = { latitude: 37, longitude: 31 };
    expect(binnedAt([older, newer, expired, elsewhere], place)).toEqual({
        row: newer,
        others: 2,
    });
    expect(binnedAt([older], place)).toEqual({ row: older, others: 0 });
    expect(binnedAt([expired, elsewhere], place)).toBeNull();
});

test("a refused field opens the earliest step that holds one and focuses its first refused field", () => {
    expect(onboardingSteps(false)).toEqual(["location", "technical", "review"]);
    expect(onboardingSteps(true)).toEqual(["technical", "review"]);
    expect(refusalTarget(["timezone"], false)).toEqual({
        step: 0,
        field: "timezone",
    });
    expect(refusalTarget(["dc_capacity_mw", "timezone"], false)).toEqual({
        step: 0,
        field: "timezone",
    });
    expect(refusalTarget(["longitude", "timezone"], false)).toEqual({
        step: 0,
        field: "timezone",
    });
    expect(refusalTarget(["name"], false)).toEqual({ step: 0, field: "name" });
    expect(refusalTarget(["name"], true)).toEqual({ step: 0, field: "name" });
    expect(refusalTarget(["export_limit_mw"], false)).toEqual({
        step: 1,
        field: "export_limit_mw",
    });
    expect(refusalTarget(["export_limit_mw"], true)).toEqual({
        step: 0,
        field: "export_limit_mw",
    });
    expect(refusalTarget(["plant.serial", "export_limit_mw"], false)).toEqual({
        step: 1,
        field: "export_limit_mw",
    });
    expect(refusalTarget(["plant.serial"], false)).toBeNull();
    expect(refusalTarget(["latitude"], true)).toBeNull();
    expect(refusalTarget([], false)).toBeNull();
});

test("a refused field no step holds is left for the general message", () => {
    expect(
        unplacedFields(
            { "plant.serial": "Unknown serial.", timezone: "Choose a zone." },
            false,
            "Check the values.",
        ),
    ).toEqual([["plant.serial", "Unknown serial."]]);
    expect(
        unplacedFields(
            { latitude: "Out of range." },
            true,
            "Check the values.",
        ),
    ).toEqual([["latitude", "Out of range."]]);
    expect(
        unplacedFields({ timezone: "Choose a zone." }, false, "Check."),
    ).toEqual([]);
});

test("a refused field's message that repeats the general message is left out, others keep their field", () => {
    expect(
        unplacedFields(
            { "plant.serial": "Unknown serial." },
            false,
            "Unknown serial.",
        ),
    ).toEqual([]);
    expect(
        unplacedFields(
            { "plant.serial": "Unknown.", "plant.model": "Unknown." },
            false,
            "Check the values.",
        ),
    ).toEqual([
        ["plant.serial", "Unknown."],
        ["plant.model", "Unknown."],
    ]);
});

test("the portfolio search reads only what a card shows", () => {
    const site = {
        name: "Old location label",
        timezone: "Europe/Istanbul",
        plant: { name: "East array" },
    };
    expect(matchesSearch(site, "east", "en")).toBe(true);
    expect(matchesSearch(site, "istanbul", "en")).toBe(true);
    expect(matchesSearch(site, "old location", "en")).toBe(false);
    expect(matchesSearch({ ...site, plant: null }, "old location", "en")).toBe(
        true,
    );
    expect(matchesSearch(site, "İSTANBUL", "tr")).toBe(true);
});
