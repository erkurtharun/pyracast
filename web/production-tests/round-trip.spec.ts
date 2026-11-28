import { expect, test, type Page } from "../tests/support/test";
import type { Response } from "@playwright/test";
import {
    firstAccount,
    mockApi,
    type AccountState,
} from "../tests/support/workspace";
import { serveSession } from "../tests/support/session";
import {
    daysAnswer,
    holderOf,
    readCopy,
    readHeld,
    refuseClears,
    seedCopy,
    workerClock,
} from "../tests/support/saved-copy";
import { workerState } from "../tests/support/worker-state";

const NOTICE_POLICY =
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'";
const FORECAST = "/app/w/1/plants/11/forecast/";
const DAYS = /\/api\/tenants\/1\/regions\/11\/plants\/21\/days\/$/;
const MINUTE = 60_000;
const SETTING = "Open figures without a connection";

type Served = { answer: object };

function daysWithDaylight() {
    const answer = daysAnswer();
    const hour = 3_600_000;
    const start = Date.parse("2026-11-11T00:00:00+03:00");
    const energy: (number | null)[] = Array.from({ length: 24 }, () => 0);
    energy.splice(7, 3, 0.5, null, 1);
    return {
        ...answer,
        forecast: answer.forecast.map((day) => ({
            ...day,
            partial: true,
            daylight_windows: [
                {
                    starts_at: "2026-11-11T06:45:00.125+03:00",
                    ends_at: "2026-11-11T10:00:00+03:00",
                },
            ],
            rows: energy.map((mwh, index) => ({
                start: new Date(start + index * hour)
                    .toISOString()
                    .replace(".000Z", "Z"),
                end: new Date(start + (index + 1) * hour)
                    .toISOString()
                    .replace(".000Z", "Z"),
                mwh,
                affected_mwh: null,
                sources: null,
            })),
        })),
    };
}

async function application(page: Page, account: AccountState, served: Served) {
    await mockApi(page, { account });
    await serveSession(page, account);
    await page.route(DAYS, (route) => route.fulfill({ json: served.answer }));
}

function totals(page: Page) {
    return readHeld(page).then(({ days }) =>
        days.map((day) => `${day.kind} ${day.date} ${day.total_mwh}`),
    );
}

async function expectFromWorker(page: Page, answer: Response | null) {
    expect(answer?.status()).toBe(503);
    expect(answer?.fromServiceWorker()).toBe(true);
    expect(answer?.headers()["content-security-policy"]).toBe(NOTICE_POLICY);
    expect(await page.evaluate(() => document.scripts.length)).toBe(0);
}

async function expectSaved(
    page: Page,
    answer: Response | null,
    kind: "Forecast" | "Production",
) {
    await expectFromWorker(page, answer);
    await expect(
        page.getByRole("heading", { name: "East array", level: 1 }),
    ).toBeVisible();
    await expect(page.locator("[aria-current=page]")).toHaveText(kind);
    await expect(page.getByText(/^Read from the server on /)).toBeVisible();
}

async function follow(page: Page, link: string) {
    const [answer] = await Promise.all([
        page.waitForResponse((response) =>
            response.request().isNavigationRequest(),
        ),
        page.getByRole("link", { name: link }).click(),
    ]);
    return answer;
}

async function expectNothingSaved(page: Page) {
    const answer = await page.goto(`${FORECAST}?date=2026-11-11`);
    expect(answer?.status()).toBe(503);
    await expect(
        page.getByRole("heading", { name: "Pyracast cannot be reached" }),
    ).toBeVisible();
    await expect(page.getByText("East array")).toHaveCount(0);
}

async function signedInWithWorker(
    page: Page,
    account: AccountState,
    served: Served,
    prepare: (now: number) => Promise<unknown> = async () => {},
) {
    await application(page, account, served);
    await page.goto("/app/");
    await page.evaluate(() => navigator.serviceWorker.ready);
    const now = await workerClock(page);
    await prepare(now);
    await page.clock.setSystemTime(now);
    account.current = firstAccount;
    await page.goto("/app/w/1/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
}

if (workerState === "figures")
    test("figures kept on the device are read without a connection, renewed once it returns, and gone after a removal and after a sign-out made without a connection", async ({
        page,
        context,
    }) => {
        const account: AccountState = { current: null };
        const served: Served = { answer: daysWithDaylight() };
        await signedInWithWorker(page, account, served);
        await expect
            .poll(() => totals(page))
            .toEqual(["forecast 2026-11-11 1.5", "production 2026-11-09 1.2"]);

        await context.setOffline(true);
        const offline = await context.newPage();
        await application(offline, account, served);
        const forecast = "Forecast";
        await expectFromWorker(offline, await offline.goto("/app/"));
        await expect(
            offline.getByRole("heading", { name: "Saved plants", level: 1 }),
        ).toBeVisible();
        await expectSaved(
            offline,
            await follow(offline, "East array"),
            forecast,
        );
        await expect(
            offline.getByText("1.5 MWh", { exact: true }),
        ).toBeVisible();
        const focusedChart = offline.getByRole("img", {
            name: /^Energy per interval of East array/,
        });
        await expect(focusedChart).toBeVisible();
        const axis = offline.locator(".chart-time:visible");
        await expect(axis).toContainText("06:00");
        await expect(axis).toContainText("10:00");
        await expect(axis).not.toContainText("00:00");
        await expectSaved(offline, await offline.reload(), forecast);
        await expect(axis).toContainText("06:00");
        await expect(
            offline.getByText("1.5 MWh", { exact: true }),
        ).toBeVisible();

        await offline.getByText("Full-day chart", { exact: true }).click();
        await expect(axis).toContainText("00:00");
        await offline.getByText("Interval values", { exact: true }).click();
        const intervals = offline.getByRole("table", {
            name: "Energy per interval",
        });
        await expect(intervals.getByRole("row")).toHaveCount(25);
        await expect(
            intervals
                .getByRole("row", { name: /06:00–07:00/ })
                .getByRole("cell", { name: "0", exact: true }),
        ).toBeVisible();
        await expect(
            intervals
                .getByRole("row", { name: /08:00–09:00/ })
                .getByRole("cell", { name: "—", exact: true }),
        ).toBeVisible();
        await expectSaved(
            offline,
            await follow(offline, "Production"),
            "Production",
        );

        const renewed = daysAnswer(2.5);
        served.answer = {
            ...renewed,
            forecast: [
                ...renewed.forecast,
                ...daysAnswer(3, "2026-11-12").forecast,
            ],
        };
        await context.setOffline(false);
        await offline.getByRole("link", { name: "Try again" }).first().click();
        await expect(
            offline.getByRole("heading", { name: "East array", level: 1 }),
        ).toBeVisible();
        const openedAt = async () =>
            (await readHeld(offline)).plants[0]?.openedAt ?? null;
        await expect.poll(openedAt).not.toBeNull();
        await offline.clock.fastForward(16 * MINUTE);
        await expect
            .poll(() => totals(offline))
            .toEqual([
                "forecast 2026-11-11 2.5",
                "forecast 2026-11-12 3",
                "production 2026-11-09 1.2",
            ]);

        await context.setOffline(true);
        await expectSaved(
            offline,
            await offline.goto(`${FORECAST}?date=2026-11-11`),
            forecast,
        );
        await expectSaved(
            offline,
            await follow(offline, "Next saved day"),
            forecast,
        );
        await expect(offline.getByText("3 MWh", { exact: true })).toBeVisible();
        await offline
            .getByRole("link", {
                name: "Remove saved figures from this device",
            })
            .click();
        await offline
            .getByRole("link", { name: "Remove saved figures", exact: true })
            .click();
        await expect(
            offline.getByText(
                "The saved figures were removed from this device.",
            ),
        ).toBeVisible();
        await expectNothingSaved(offline);

        await context.setOffline(false);
        account.current = { ...firstAccount, tag: "tag-c" };
        await page.reload();
        await expect
            .poll(() => totals(page))
            .toEqual([
                "forecast 2026-11-11 2.5",
                "forecast 2026-11-12 3",
                "production 2026-11-09 1.2",
            ]);
        await context.setOffline(true);
        await page.route("**/api/auth/browser/v1/auth/session", (route) =>
            route.request().method() === "DELETE"
                ? route.abort("internetdisconnected")
                : route.fallback(),
        );
        await page
            .getByRole("button", { name: "Sign out", exact: true })
            .click();
        await expect(
            page.getByRole("heading", { name: "Signing out…" }),
        ).toBeVisible();
        await expect(
            page.getByText("It is not known whether it was saved"),
        ).toBeVisible();
        await expectNothingSaved(offline);
    });

if (workerState === "figuresWithdrawn")
    test("a build whose saved figures are withdrawn shows no setting and reads no figures, also over a copy it could not remove", async ({
        page,
    }) => {
        const account: AccountState = { current: null };
        const reads: string[] = [];
        page.on("request", (request) => {
            if (DAYS.test(request.url())) reads.push(request.url());
        });
        await signedInWithWorker(
            page,
            account,
            { answer: daysAnswer() },
            async (now) => {
                await seedCopy(page, {
                    holder: holderOf({ confirmedAt: now }),
                });
                await refuseClears(page);
            },
        );
        expect(await readCopy(page)).toMatchObject({ plants: 1 });
        for (let step = 0; step < 12; step += 1)
            await page.clock.fastForward(5 * MINUTE);
        await page
            .getByRole("button", { name: "Account", exact: true })
            .click();
        await expect(
            page.getByRole("heading", {
                name: "Account",
                exact: true,
                level: 1,
            }),
        ).toBeVisible();
        await expect(page.getByText(SETTING, { exact: true })).toHaveCount(0);
        expect(reads).toEqual([]);
    });
