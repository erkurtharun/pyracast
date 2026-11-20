import { expect, test } from "./support/test";
import { mockApi, paged, plant, region, user } from "./support/workspace";
import { nativeProductionHistory } from "./support/native-production";
import type { components } from "../src/lib/schema";

for (const role of ["admin", "viewer"] as const) {
    test(`native production distinguishes repeated daylight-saving hours and compacts ordinary days (${role})`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
        await mockApi(page);
        await page.route("**/api/tenants/", (route) =>
            route.fulfill({
                json: paged([{ id: 1, name: "Sunfield Energy", role }]),
            }),
        );
        await page.route("**/api/tenants/1/sites/", (route) =>
            route.fulfill({
                json: paged([{ ...region, timezone: "Europe/Berlin", plant }]),
            }),
        );
        const saved = new Map<
            string,
            components["schemas"]["ObservationOutput"]
        >();
        const writes: string[] = [];
        await page.route("**/plants/21/production/?**", async (route) => {
            const query = new URL(route.request().url()).searchParams;
            expect(query.get("native_intervals")).toBe("true");
            const date = query.get("start_date")!;
            const intervals = ["00", "01"].map((hour) => {
                const starts_at = `${date}T${hour}:00:00Z`;
                const observation = saved.get(starts_at) ?? null;
                return {
                    starts_at,
                    ends_at: `${date}T${hour === "00" ? "01" : "02"}:00:00Z`,
                    observation,
                    revision_id: observation?.id ?? null,
                    can_revert: observation !== null,
                    effective: observation
                        ? {
                              energy_mwh: observation.energy_mwh,
                              quality: observation.quality,
                              source: observation.source,
                              source_kind: "manual",
                              provisional: false,
                              aggregated: false,
                          }
                        : null,
                };
            });
            await route.fulfill({
                json: nativeProductionHistory(date, "Europe/Berlin", intervals),
            });
        });
        await page.route("**/plants/21/production/changes/", async (route) => {
            expect(role).toBe("admin");
            expect(route.request().method()).toBe("POST");
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            const body = route.request().postDataJSON();
            const index = writes.length;
            expect(body).toEqual({
                configuration_id: 31,
                confirm_implausible: false,
                unit: "MWh",
                changes: [
                    {
                        starts_at: `2026-10-25T0${index}:00:00Z`,
                        ends_at: `2026-10-25T0${index + 1}:00:00Z`,
                        expected_revision_id: null,
                        action: "set",
                        energy: index === 0 ? 0 : 1.25,
                        quality: "valid",
                        source: "meter",
                    },
                ],
            });
            const change = body.changes[0];
            saved.set(change.starts_at, {
                id: index + 1,
                configuration_id: 31,
                starts_at: change.starts_at,
                ends_at: change.ends_at,
                energy_mwh: change.energy,
                quality: "valid",
                source: "meter",
                recorded_by_id: user.id,
                created_at: "2026-11-10T12:00:00Z",
                supersedes_id: null,
                is_reverted: false,
            });
            writes.push(change.starts_at);
            await route.fulfill({
                json: { created: 1, existing: 0, count: 1, ids: [index + 1] },
            });
        });
        await page.goto("/app/");
        await page
            .getByRole("button", { name: /East array|Antalya solar park/ })
            .click();
        await page
            .getByRole("button", { name: "Production", exact: true })
            .click();
        const day = page.getByLabel("Day", { exact: true });
        await day.fill("2026-10-25");
        const table = page.getByRole("table", {
            name: "Production intervals",
        });
        const fullDay = page.getByRole("checkbox", { name: "Show full day" });
        await expect(fullDay).not.toBeChecked();
        await fullDay.check();
        const labels = ["02:00 GMT+2–02:00 GMT+1", "02:00 GMT+1–03:00 GMT+1"];
        for (const label of labels)
            await expect(
                table.getByRole("rowheader", { name: label, exact: true }),
            ).toBeVisible();
        if (role === "admin") {
            for (const [index, label] of labels.entries()) {
                await table
                    .getByRole("button", { name: `Edit ${label}`, exact: true })
                    .click();
                const editor = page.locator(".production-row-editor");
                await editor
                    .getByRole("textbox")
                    .fill(index === 0 ? "0" : "1.25");
                await editor
                    .getByRole("button", { name: "Save", exact: true })
                    .click();
                await expect(editor).toHaveCount(0);
                await expect.poll(() => writes.length).toBe(index + 1);
            }
        } else {
            await expect(
                table.getByRole("button", { name: /^Edit / }),
            ).toHaveCount(0);
        }
        await day.fill("2026-10-24");
        await expect(
            table.getByRole("rowheader", { name: "02:00–03:00", exact: true }),
        ).toBeVisible();
        await expect(
            table.getByRole("rowheader", { name: "03:00–04:00", exact: true }),
        ).toBeVisible();
        await expect(table.getByRole("rowheader", { name: /GMT/ })).toHaveCount(
            0,
        );
        await day.fill("2026-10-25");
        for (const label of labels)
            await expect(
                table.getByRole("rowheader", { name: label, exact: true }),
            ).toBeVisible();
        if (role === "admin") {
            const values = table.locator(".production-value");
            await expect(values.nth(0)).toHaveText(/^0\s/);
            await expect(values.nth(1)).toHaveText(/^1\.25\s/);
            expect(writes).toEqual([
                "2026-10-25T00:00:00Z",
                "2026-10-25T01:00:00Z",
            ]);
        } else expect(writes).toEqual([]);
    });
}
