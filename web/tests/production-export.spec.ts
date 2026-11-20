import { readFile } from "node:fs/promises";
import { expect, test } from "./support/test";
import { mockApi } from "./support/workspace";

test("an empty production day downloads one row of metadata", async ({
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
    const text = await readFile((await (await download).path())!, "utf8");
    expect(text.replace(/^﻿/, "").split("\r\n")).toEqual([
        '"timezone","unit"',
        '"Europe/Istanbul","MWh"',
        "",
    ]);
});
