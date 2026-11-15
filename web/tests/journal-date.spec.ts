import { expect, test } from "./support/test";
import { mockApi, paged, region } from "./support/workspace";
import { nativeProductionHistory } from "./support/native-production";

for (const role of ["admin", "viewer"] as const) {
    test(`production dates use the plant day and retain selection after a late response (${role})`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(new Date("2026-01-08T21:30:00Z"));
        await mockApi(page);
        await page.route("**/api/tenants/", (route) =>
            route.fulfill({
                json: paged([{ id: 1, name: "Sunfield Energy", role }]),
            }),
        );
        let release!: () => void;
        let started!: () => void;
        const pending = new Promise<void>((resolve) => {
            release = resolve;
        });
        const initialStarted = new Promise<void>((resolve) => {
            started = resolve;
        });
        let first = true;
        const dates: string[] = [];
        const writes: string[] = [];
        page.on("request", (request) => {
            if (
                request.url().includes("/api/tenants/") &&
                request.method() !== "GET"
            )
                writes.push(request.url());
        });
        await page.route("**/plants/21/production/?**", async (route) => {
            const query = new URL(route.request().url()).searchParams;
            expect(query.get("native_intervals")).toBe("true");
            const date = query.get("start_date")!;
            expect(query.get("end_date")).toBe(date);
            dates.push(date);
            if (first) {
                first = false;
                expect(date).toBe("2026-01-08");
                started();
                await pending;
            }
            const hour = date === "2026-01-08" ? "08" : "09";
            await route.fulfill({
                json: nativeProductionHistory(date, region.timezone, [
                    {
                        starts_at: `${date}T${hour}:00:00Z`,
                        ends_at: `${date}T${String(Number(hour) + 1).padStart(2, "0")}:00:00Z`,
                    },
                ]),
            });
        });
        await page.goto("/");
        await page
            .getByRole("button", { name: /East array|Antalya solar park/ })
            .click();
        await page
            .getByRole("button", { name: "Production", exact: true })
            .click();
        await initialStarted;
        const day = page.getByLabel("Day", { exact: true });
        await expect(day).toHaveValue("2026-01-08");
        await expect(day).toHaveAttribute("max", "2026-01-08");
        await expect(day).toBeEnabled();
        const table = page.getByRole("table", {
            name: "Production intervals · Europe/Istanbul",
        });
        try {
            await day.fill("2026-01-07");
            await expect(
                table.getByRole("rowheader", {
                    name: "12:00–13:00",
                    exact: true,
                }),
            ).toBeVisible();
            const initialResponse = page.waitForResponse(
                (response) =>
                    response.url().includes("/production/?") &&
                    new URL(response.url()).searchParams.get("start_date") ===
                        "2026-01-08",
            );
            release();
            await (await initialResponse).finished();
            await expect(day).toHaveValue("2026-01-07");
            await expect(
                table.getByRole("rowheader", {
                    name: "12:00–13:00",
                    exact: true,
                }),
            ).toBeVisible();
            await expect(
                table.getByRole("rowheader", {
                    name: "11:00–12:00",
                    exact: true,
                }),
            ).toHaveCount(0);
            if (role === "admin") {
                await table
                    .getByRole("button", {
                        name: "Edit 12:00–13:00",
                        exact: true,
                    })
                    .click();
                const editor = page.locator(".production-row-editor");
                await editor.getByRole("textbox").fill("0");
                await expect(
                    editor.getByRole("button", { name: "Save", exact: true }),
                ).toBeEnabled();
                await editor
                    .getByRole("button", { name: "Cancel", exact: true })
                    .click();
            } else {
                await expect(
                    table.getByRole("button", { name: /^Edit / }),
                ).toHaveCount(0);
            }
            await page
                .getByRole("button", { name: "Previous day", exact: true })
                .click();
            await expect(day).toHaveValue("2026-01-06");
            await page
                .getByRole("button", { name: "Next day", exact: true })
                .click();
            await expect(day).toHaveValue("2026-01-07");
            await page
                .getByRole("button", { name: "Next day", exact: true })
                .click();
            await expect(day).toHaveValue("2026-01-08");
            await expect(
                page.getByRole("button", { name: "Next day", exact: true }),
            ).toBeDisabled();
            await day.fill("2026-01-09");
            await expect(day).toHaveValue("2026-01-08");
            expect(dates).not.toContain("2026-01-09");
            expect(writes).toEqual([]);
        } finally {
            release();
        }
    });
}
