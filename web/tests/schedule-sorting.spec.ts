import { expect, test } from "./support/test";
import { mockApi } from "./support/workspace";

test("reminder times are deduplicated and sent in clock order", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page.getByRole("button", { name: "Email reminders" }).click();
    await page.getByLabel("Enable forecast emails for this region").check();
    await page.getByLabel("Delivery times").fill("10:00, 09:30, 09:00, 09:30");
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
