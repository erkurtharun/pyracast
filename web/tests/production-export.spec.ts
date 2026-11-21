import { readFile } from "node:fs/promises";
import { expect, test } from "./support/test";
import { mockApi } from "./support/workspace";

test("an empty production day downloads the import header and no fabricated row", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-01-08T09:00:00Z"));
    await mockApi(page);
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download CSV" }).click();
    const file = await download;
    expect(file.suggestedFilename()).toBe(
        "production-East array-2026-01-07.csv",
    );
    const text = await readFile(await file.path(), "utf8");
    expect(text.replace(/^\uFEFF/, "").split("\r\n")).toEqual([
        '"starts_at","ends_at","energy_mwh","quality","source","timezone"',
        "",
    ]);
});
