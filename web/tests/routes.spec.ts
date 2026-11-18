import { expect, test } from "@playwright/test";
import {
    forecastAddress,
    isPlantSection,
    routeId,
    routes,
} from "../src/lib/routes";

test("every application screen has one address built in one place", () => {
    expect(routes.entry()).toBe("/app/");
    expect(routes.account()).toBe("/app/account/");
    expect(routes.invitation()).toBe("/app/invitation/");
    expect(routes.workspaces()).toBe("/app/w/");
    expect(routes.portfolio(5)).toBe("/app/w/5/");
    expect(routes.members(5)).toBe("/app/w/5/members/");
    expect(routes.inbox(5)).toBe("/app/w/5/inbox/");
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

test("the forecast address keeps only a valid date and view", () => {
    expect(
        forecastAddress(
            new URLSearchParams("date=2026-11-20&view=irradiance&extra=1"),
        ),
    ).toEqual({ date: "2026-11-20", view: "irradiance" });
    expect(
        forecastAddress(new URLSearchParams("date=2026-13-40&view=chart")),
    ).toEqual({});
    expect(forecastAddress(new URLSearchParams("date=20261120"))).toEqual({});
});
