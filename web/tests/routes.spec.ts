import { expect, test } from "@playwright/test";
import {
    addressWithout,
    appQuery,
    callbackParams,
    isPlantSection,
    plantAddress,
    plantView,
    routeId,
    routes,
    screenOf,
} from "../src/lib/routes";

test("every application screen has one address built in one place", () => {
    expect(routes.entry()).toBe("/app/");
    expect(routes.signIn("tr")).toBe("/app/?language=tr");
    expect(routes.signUp("en")).toBe("/app/?signup=1&language=en");
    expect(routes.socialCallback()).toBe("/app/?social_callback=1");
    expect(routes.account()).toBe("/app/account/");
    expect(routes.invitation()).toBe("/app/invitation/");
    expect(routes.workspaces()).toBe("/app/w/");
    expect(routes.portfolio(5)).toBe("/app/w/5/");
    expect(routes.members(5)).toBe("/app/w/5/members/");
    expect(routes.inbox(5)).toBe("/app/w/5/inbox/");
    expect(routes.inbox(5, 7)).toBe("/app/w/5/inbox/?request=7");
    expect(routes.trash(5)).toBe("/app/w/5/trash/");
    expect(routes.plant(5, 11)).toBe("/app/w/5/plants/11/forecast/");
    expect(routes.plant(5, 11, "settings")).toBe(
        "/app/w/5/plants/11/settings/",
    );
    expect(
        routes.plant(5, 11, "forecast", {
            date: "2026-11-20",
            view: "irradiance",
        }),
    ).toBe("/app/w/5/plants/11/forecast/?date=2026-11-20&view=irradiance");
    expect(routes.plant(5, 11, "forecast", { view: "generation" })).toBe(
        "/app/w/5/plants/11/forecast/",
    );
    expect(
        routes.plant(5, 11, "production", {
            date: "2026-11-18",
            view: "comparison",
        }),
    ).toBe("/app/w/5/plants/11/production/?date=2026-11-18&view=comparison");
    expect(routes.plant(5, 11, "production", { view: "actuals" })).toBe(
        "/app/w/5/plants/11/production/",
    );
    expect(
        routes.plant(5, 11, "settings", {
            date: "2026-11-18",
            view: "collection",
        }),
    ).toBe("/app/w/5/plants/11/settings/?view=collection");
    expect(
        routes.plant(5, 11, "calibration", { date: "2026-11-18", view: "x" }),
    ).toBe("/app/w/5/plants/11/calibration/");
});

test("the screen of an address names the active navigation entry", () => {
    expect(screenOf("/app/account/")).toBe("account");
    expect(screenOf("/app/w/5/")).toBe("portfolio");
    expect(screenOf("/app/w/5/plants/11/forecast/")).toBe("plant");
    expect(screenOf("/app/w/5/members/")).toBe("members");
    expect(screenOf("/app/w/5/inbox/")).toBe("inbox");
    expect(screenOf("/app/w/5/trash/")).toBe("trash");
    expect(screenOf("/app/w/")).toBe("other");
    expect(screenOf("/app/w/5/unknown/")).toBe("other");
});

test("route segments accept only positive ids and known plant sections", () => {
    expect(routeId("12")).toBe(12);
    for (const value of ["0", "-1", "1e3", "12a", "", " 12", "01", undefined])
        expect(routeId(value), String(value)).toBeUndefined();
    for (const section of [
        "forecast",
        "production",
        "calibration",
        "outages",
        "settings",
    ])
        expect(isPlantSection(section)).toBe(true);
    for (const section of ["members", "Forecast", "", undefined])
        expect(isPlantSection(section)).toBe(false);
});

test("a plant address keeps only a valid date and a view of its section", () => {
    const query = (value: string) => new URLSearchParams(value);
    expect(
        plantAddress(
            "forecast",
            query("date=2026-11-20&view=irradiance&extra=1"),
        ),
    ).toEqual({ date: "2026-11-20", view: "irradiance" });
    expect(
        plantAddress("forecast", query("date=2026-13-40&view=chart")),
    ).toEqual({});
    expect(plantAddress("forecast", query("date=20261120"))).toEqual({});
    expect(
        plantAddress("production", query("date=2026-11-18&view=comparison")),
    ).toEqual({ date: "2026-11-18", view: "comparison" });
    expect(plantAddress("production", query("view=irradiance"))).toEqual({});
    expect(
        plantAddress("settings", query("date=2026-11-18&view=collection")),
    ).toEqual({ view: "collection" });
    expect(
        plantAddress("calibration", query("date=2026-11-18&view=actuals")),
    ).toEqual({});
});

test("a plant view falls back to the first view of its section", () => {
    expect(plantView("forecast", {})).toBe("generation");
    expect(plantView("forecast", { view: "comparison" })).toBe("generation");
    expect(plantView("production", { view: "comparison" })).toBe("comparison");
    expect(plantView("settings", {})).toBe("configuration");
    expect(plantView("settings", { view: "reminders" })).toBe("reminders");
});

test("one parser reads the entry query and the social callback", () => {
    const query = (value: string) => appQuery(new URLSearchParams(value));
    expect(query("")).toEqual({ signup: false, socialCallback: "none" });
    expect(query("signup=1&language=tr")).toEqual({
        signup: true,
        language: "tr",
        socialCallback: "none",
    });
    expect(query("signup=yes&language=de")).toEqual({
        signup: false,
        socialCallback: "none",
    });
    expect(query("social_callback=1")).toEqual({
        signup: false,
        socialCallback: "succeeded",
    });
    expect(
        query("social_callback=1&error=cancelled&error_process=connect"),
    ).toEqual({ signup: false, socialCallback: "failed" });
    expect(query("error=cancelled")).toEqual({
        signup: false,
        socialCallback: "none",
    });
});

test("consumed parameters leave the address and keep the rest", () => {
    expect(
        addressWithout(
            new URL(
                "https://pyracast.test/app/account/?social_callback=1&error=x&error_process=connect&keep=1#part",
            ),
            callbackParams,
        ),
    ).toBe("/app/account/?keep=1#part");
    expect(
        addressWithout(
            new URL("https://pyracast.test/app/?language=tr&signup=1"),
            ["language"],
        ),
    ).toBe("/app/?signup=1");
    expect(
        addressWithout(new URL("https://pyracast.test/app/?language=tr"), [
            "language",
        ]),
    ).toBe("/app/");
});
