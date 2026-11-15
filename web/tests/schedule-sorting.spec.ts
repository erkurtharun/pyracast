import { expect, test } from "./support/test";
import { mockApi } from "./support/workspace";

test("reminder times are deduplicated and sent in clock order", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Irradiance", exact: true }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Email reminders" }).click();
    await page.getByLabel("Enable forecast emails for this plant").check();
    for (const [index, time] of [
        "10:00",
        "09:30",
        "09:00",
        "09:30",
    ].entries()) {
        if (index)
            await page
                .getByRole("button", { name: "Add time", exact: true })
                .click();
        await page
            .getByLabel(`Delivery time ${index + 1}`, { exact: true })
            .fill(time);
    }
    const outgoing = page.waitForRequest(
        (request) =>
            request.method() === "PUT" && request.url().includes("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await outgoing).postDataJSON().times).toEqual([
        "09:00",
        "09:30",
        "10:00",
    ]);
});
