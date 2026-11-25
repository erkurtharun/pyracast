import { expect, test } from "@playwright/test";
import {
    checkPage,
    recordCspViolations,
    signInToConsole,
} from "./support/console";

test("a platform administrator creates a workspace, adds a plant and manages members", async ({
    page,
}, testInfo) => {
    test.setTimeout(130_000);
    const violations = await recordCspViolations(page);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await signInToConsole(
        page,
        process.env.BROWSER_CONSOLE_EMAIL!,
        process.env.BROWSER_ADMIN_PASSWORD!,
        process.env.BROWSER_CONSOLE_RECOVERY_CODES!,
    );

    await page.goto("/admin/workspaces/");
    await checkPage(page, testInfo, "console-workspaces-desktop");
    await page.getByRole("link", { name: "New workspace" }).first().click();
    await expect(
        page.getByRole("heading", { name: "New workspace", exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Create workspace" }).click();
    await expect(page.getByLabel(/^Workspace name/)).toBeFocused();
    await page.getByLabel(/^Workspace name/).fill("Browser console customer");
    await page
        .getByLabel(/^Workspace administrator email/)
        .fill("browser-owner@example.test");
    await page.getByRole("button", { name: "Create workspace" }).click();
    await expect(
        page.getByRole("heading", {
            level: 1,
            name: "Browser console customer",
        }),
    ).toBeVisible();
    await expect(
        page.getByRole("status").filter({
            hasText:
                "The invitation to browser-owner@example.test is being sent; its state appears in the list.",
        }),
    ).toBeVisible();
    await checkPage(page, testInfo, "console-workspace-desktop");
    await page.setViewportSize({ width: 320, height: 800 });
    await checkPage(page, testInfo, "console-workspace-mobile");

    await page.getByRole("link", { name: "Add plant" }).first().click();
    await page.getByLabel(/^Plant name/).fill("Browser console plant");
    await expect(
        page.getByText("Paste a pair such as 41.0082, 28.9784 into Latitude"),
    ).toBeVisible();
    const latitude = page.getByLabel(/^Latitude/);
    await expect(latitude).not.toHaveAttribute("inputmode");
    await expect(page.getByLabel(/^Longitude/)).not.toHaveAttribute(
        "inputmode",
    );
    const pasteCoordinates = (text: string) =>
        latitude.evaluate((element, text) => {
            const data = new DataTransfer();
            data.setData("text/plain", text);
            const event = new ClipboardEvent("paste", {
                clipboardData: data,
                bubbles: true,
                cancelable: true,
            });
            element.dispatchEvent(event);
            return event.defaultPrevented;
        }, text);
    for (const [text, expectedLatitude, expectedLongitude] of [
        [" -41.5 ; 28.9 ", "-41.5", "28.9"],
        ["41.5\t-28.9", "41.5", "-28.9"],
        ["41.0082, 28.9784", "41.0082", "28.9784"],
        ["41,0082; 28,9784", "41,0082", "28,9784"],
    ] as const) {
        expect(await pasteCoordinates(text)).toBe(true);
        await expect(latitude).toHaveValue(expectedLatitude);
        await expect(page.getByLabel(/^Longitude/)).toHaveValue(
            expectedLongitude,
        );
    }
    for (const text of ["41,,29", "41 29 30", "41. 29", "NaN,29"]) {
        expect(await pasteCoordinates(text)).toBe(false);
    }
    await expect(latitude).toHaveValue("41,0082");
    await expect(page.getByLabel(/^Longitude/)).toHaveValue("28,9784");
    await page.getByLabel(/^Panel capacity/).fill("12");
    const tilt = page.getByLabel(/^Panel tilt/);
    await expect(tilt).toBeHidden();
    await page.getByLabel(/^Mounting/).selectOption("fixed");
    await expect(tilt).toBeVisible();
    await tilt.fill("25");
    await page.getByLabel(/^Panel azimuth/).fill("180");
    await checkPage(page, testInfo, "console-plant-form-mobile");
    await page.getByRole("button", { name: "Add plant" }).click();
    await expect(page).toHaveURL(/\/admin\/plants\/\d+\/$/);
    await expect(
        page
            .getByRole("status")
            .filter({ hasText: "Plant Browser console plant created." }),
    ).toBeVisible();
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole("link", { name: "Edit plant" }).click();
    await expect(
        page.getByRole("heading", { name: "Edit plant", exact: true }),
    ).toBeVisible();
    await checkPage(page, testInfo, "console-plant-details-desktop");

    await page.goto(`/admin/workspaces/${process.env.BROWSER_OTHER_TENANT!}/`);
    await page.getByRole("link", { name: "Invite member" }).click();
    await page.getByLabel(/^Email address/).fill("browser-viewer@example.test");
    await page.getByRole("button", { name: "Send invitation" }).click();
    await expect(
        page.getByRole("status").filter({
            hasText:
                "The invitation to browser-viewer@example.test is being sent; its state appears in the list.",
        }),
    ).toBeVisible();
    const member = process.env.BROWSER_REMOVABLE_MEMBER!;
    await page
        .getByRole("link", { name: `Remove from workspace ${member}` })
        .click();
    await page.setViewportSize({ width: 320, height: 800 });
    await checkPage(page, testInfo, "console-member-removal-mobile");
    await page.getByRole("button", { name: "Remove from workspace" }).click();
    await expect(
        page
            .getByRole("status")
            .filter({ hasText: `${member} was removed from the workspace.` }),
    ).toBeVisible();
    await checkPage(page, testInfo, "console-members-mobile");
    expect(violations).toEqual([]);
});
