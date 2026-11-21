import { expect, test } from "@playwright/test";
import {
    coordinatePair,
    sameLocation,
    zoneChoices,
} from "../src/features/portfolio/site-location";
import {
    onboardingSteps,
    stepOfField,
} from "../src/features/portfolio/onboarding-steps";
import { matchesSearch } from "../src/features/portfolio/portfolio-search";

test("a pasted coordinate pair splits into latitude and longitude", () => {
    expect(coordinatePair(" -41.5 ; 28.9 ")).toEqual(["-41.5", "28.9"]);
    expect(coordinatePair("41.5\t-28.9")).toEqual(["41.5", "-28.9"]);
    expect(coordinatePair("41.0082, 28.9784")).toEqual(["41.0082", "28.9784"]);
    for (const text of ["41,,29", "41 29 30", "41. 29", "NaN,29", "41"])
        expect(coordinatePair(text)).toBeNull();
});

test("the zone list holds every supported zone and the current one once, sorted", () => {
    expect(
        zoneChoices(["Europe/Istanbul", "Asia/Tokyo"], "Asia/Calcutta"),
    ).toEqual(["Asia/Calcutta", "Asia/Tokyo", "Europe/Istanbul"]);
    expect(zoneChoices(["UTC", "Europe/Istanbul"], "UTC")).toEqual([
        "Europe/Istanbul",
        "UTC",
    ]);
});

test("a location matches a stored one within the stored precision", () => {
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
});

test("a refused field opens the step that holds it", () => {
    expect(onboardingSteps(false)).toEqual(["location", "technical", "review"]);
    expect(onboardingSteps(true)).toEqual(["technical", "review"]);
    expect(stepOfField(["timezone"], false)).toBe(0);
    expect(stepOfField(["latitude", "dc_capacity_mw"], false)).toBe(0);
    expect(stepOfField(["name"], false)).toBe(0);
    expect(stepOfField(["name"], true)).toBe(0);
    expect(stepOfField(["export_limit_mw"], false)).toBe(1);
    expect(stepOfField(["export_limit_mw"], true)).toBe(0);
    expect(stepOfField([], false)).toBeNull();
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
