import { type Page } from "./test";
import { answer, mockApi, paged, tenantRow, user } from "./workspace";
import { nativeProductionHistory } from "./native-production";
import type { components } from "../../src/lib/schema";

type Schemas = components["schemas"];
type Row = Partial<Parameters<typeof nativeProductionHistory>[2][number]>;
type Language = "en" | "tr";

export const now = new Date("2026-11-10T12:00:00Z");

const reading = {
    energy_mwh: 1,
    quality: "valid",
    source: "meter",
    source_kind: "manual",
    provisional: false,
    aggregated: false,
} as const;

export async function signIn(
    page: Page,
    { language = "en" }: { language?: Language } = {},
) {
    let current = language;
    const session = () => ({
        user: { ...user, language_preference: current },
        language: current,
        csrf_token: "signed-token",
    });
    await mockApi(page);
    await page.route("**/api/session/", (route) =>
        route.fulfill({ json: session() }),
    );
    await page.route("**/api/session/language/", (route) => {
        current = (route.request().postDataJSON() as { language: Language })
            .language;
        return route.fulfill({ json: session() });
    });
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: "admin" }),
            ]),
        }),
    );
    await page.route("**/change-requests/?*", (route) =>
        route.fulfill({ json: paged([]) }),
    );
    return { language: () => current };
}

function productionDay(date: string, rows: Row[]) {
    return nativeProductionHistory(
        date,
        "Europe/Istanbul",
        rows.map((row, index) => {
            const minutes = 8 * 60 + index * 30;
            const clock = (value: number) =>
                `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
            return {
                starts_at: `${date}T${clock(minutes)}:00+03:00`,
                ends_at: `${date}T${clock(minutes + 30)}:00+03:00`,
                effective: reading,
                ...row,
            };
        }),
    );
}

export async function serveProduction(page: Page, rows: Row[]) {
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({ json: productionDay(date, rows) });
    });
}

export async function openPlant(page: Page, section?: string) {
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    if (section)
        await page.getByRole("button", { name: section, exact: true }).click();
}

export async function loseConnection(page: Page) {
    let lost = true;
    await page.route("**/api/**", (route) =>
        lost ? route.abort("internetdisconnected") : route.fallback(),
    );
    await page.evaluate(() => window.dispatchEvent(new Event("offline")));
    return async () => {
        lost = false;
        await page.evaluate(() => window.dispatchEvent(new Event("online")));
    };
}

export async function focusWindow(page: Page) {
    await page.evaluate(() =>
        document.dispatchEvent(
            new Event("visibilitychange", { bubbles: true }),
        ),
    );
}

type Outage = Schemas["IncidentListOutput"];

export const activeOutage: Outage = {
    id: 51,
    kind: "inverter",
    title: "Inverter disconnected",
    status: "active",
    revision: 1,
    starts_at: "2026-11-09T08:00:00Z",
    ends_at: null,
    available_dc_mw: 0,
    available_inverter_mw: 0,
    available_export_mw: 0,
    declared_configuration_id: 31,
    notes: "",
    created_at: "2026-11-09T08:00:00Z",
    updated_at: "2026-11-09T08:00:00Z",
    pending_request_id: null,
};

export async function serveOutages(page: Page, outages: Outage[]) {
    await page.route("**/plants/21/outages/", (route) =>
        route.request().method() === "GET"
            ? route.fulfill({
                  json: answer("IncidentPage", {
                      ...paged(outages),
                      can_propose: false,
                      proposal_unavailable_reason: "admin_edits_directly",
                  }),
              })
            : route.fallback(),
    );
}
