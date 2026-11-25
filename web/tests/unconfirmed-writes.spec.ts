import type { Locator, Route } from "@playwright/test";
import { expect, test, type Page } from "./support/test";
import { errorBody } from "./support/errors";
import {
    accountWith,
    mockApi,
    openReminders,
    paged,
    plant,
    region,
    reminderApi,
    sessionIdentity,
} from "./support/workspace";
import {
    activeOutage,
    answers,
    focusWindow,
    now,
    openPlant,
    reviewRequest,
    serveDirectProduction,
    serveHalfHourCorrection,
    serveImport,
    serveOutageProposals,
    serveOutageWrites,
    serveProduction,
    serveProposals,
    serveSplitSlot,
    signIn,
    type Answering,
} from "./support/plant-screens";

test.use({ timezoneId: "Europe/Istanbul" });

type Language = "en" | "tr";

const texts = {
    en: {
        title: "Change not confirmed",
        repeatable:
            "The server did not confirm the change. It may have been saved. Your entry is still here: sending it again unchanged saves it only once.",
        general:
            "The server did not confirm the change. It may have been saved; check before you send it again.",
        refused: "We couldn’t save your changes",
    },
    tr: {
        title: "Değişiklik onaylanmadı",
        repeatable:
            "Sunucu değişikliği onaylamadı. Değişiklik kaydedilmiş olabilir. Girdiğiniz bilgiler hâlâ burada: değiştirmeden yeniden gönderirseniz yalnızca bir kez kaydedilir.",
        general:
            "Sunucu değişikliği onaylamadı. Değişiklik kaydedilmiş olabilir; yeniden göndermeden önce kontrol edin.",
        refused: "Değişiklikler kaydedilemedi",
    },
} as const;

const labels = {
    en: {
        production: "Production",
        outages: "Outages",
        edit: /^Edit 08:00/,
        save: "Save",
        suggest: /^Edit 08:00/,
        add: "Add to request",
        draft: "Correction request",
        reason: "Reason for correction",
        send: "Send for approval",
        importCsv: "Import CSV",
        csv: "CSV file",
        preview: "Preview",
        confirm: "Import 5 values",
        imported: "Imported. New values: 4 · Already saved: 1",
        report: "Report outage",
        title: "Incident title",
        saveOutage: "Save outage",
        editReport: "Edit report",
        saveChanges: "Save outage changes",
        resolve: "Resolve outage",
        saveResolution: "Save resolution",
        cancelEntry: "Cancel an incorrect entry",
        confirmCancel: "I confirm this incident was recorded incorrectly.",
        cancelOutage: "Cancel outage",
        proposeResolution: "Submit resolution",
        optionalReason: "Reason (optional)",
        sent: "Sent for approval. Live data stays unchanged until a workspace administrator approves it.",
        closed: (status: string) =>
            `This request is already closed (${status}); this submission changed nothing.`,
        statuses: {
            approved: "Approved",
            rejected: "Rejected",
            withdrawn: "Withdrawn",
        },
    },
    tr: {
        production: "Üretim",
        outages: "Kesintiler",
        edit: /^08:00.+aralığını düzenle$/,
        save: "Kaydet",
        suggest: /^08:00.+aralığını düzenle$/,
        add: "Talebe ekle",
        draft: "Düzeltme talebi",
        reason: "Düzeltme gerekçesi",
        send: "Onaya gönder",
        importCsv: "CSV içe aktar",
        csv: "CSV dosyası",
        preview: "Önizle",
        confirm: "5 değeri içe aktar",
        imported: "İçe aktarıldı. Yeni değer: 4 · Zaten kayıtlı: 1",
        report: "Kesinti bildir",
        title: "Olay başlığı",
        saveOutage: "Kesintiyi kaydet",
        editReport: "Bildirimi düzenle",
        saveChanges: "Kesinti değişikliklerini kaydet",
        resolve: "Kesintiyi sonlandır",
        saveResolution: "Sonlandırmayı kaydet",
        cancelEntry: "Hatalı kaydı iptal et",
        confirmCancel: "Bu olayın hatalı kaydedildiğini onaylıyorum.",
        cancelOutage: "Kesintiyi iptal et",
        proposeResolution: "Çözüm bildir",
        optionalReason: "Gerekçe (isteğe bağlı)",
        sent: "Onaya gönderildi. Çalışma alanı yöneticisi onaylayana kadar canlı veriler değişmez.",
        closed: (status: string) =>
            `Bu talep zaten kapandı (${status}); bu gönderim hiçbir şeyi değiştirmedi.`,
        statuses: {
            approved: "Onaylandı",
            rejected: "Reddedildi",
            withdrawn: "Geri çekildi",
        },
    },
} as const;

async function expectUnconfirmed(
    where: Locator,
    repeatable: boolean,
    language: Language = "en",
) {
    const said = texts[language];
    const alert = where.getByRole("alert").filter({ hasText: said.title });
    await expect(alert).toHaveCount(1);
    await expect(alert).toContainText(
        repeatable ? said.repeatable : said.general,
    );
    await expect(alert).not.toContainText(
        repeatable ? said.general : said.repeatable,
    );
    await expect(alert).not.toContainText(said.refused);
}

async function holdReads(page: Page, url: string | RegExp) {
    let release = () => {};
    const released = new Promise<void>((resolve) => {
        release = resolve;
    });
    await page.route(url, async (route) => {
        if (route.request().method() === "GET") await released;
        return route.fallback();
    });
    return release;
}

async function holdWrites(page: Page, url: string) {
    let release = () => {};
    const released = new Promise<void>((resolve) => {
        release = resolve;
    });
    let holding = false;
    await page.route(url, async (route) => {
        if (holding && route.request().method() !== "GET") await released;
        return route.fallback();
    });
    return {
        hold: () => {
            holding = true;
        },
        release: () => {
            holding = false;
            release();
        },
    };
}

async function readAgainOnFocus(page: Page) {
    await page.clock.runFor(5_000);
    await focusWindow(page);
}

// A focus joins a read on its way; the export is enabled again once the reads a lost outage change started answered.
async function outageReadsAnswered(page: Page) {
    await expect(
        page.getByRole("button", { name: "Download outages CSV", exact: true }),
    ).toBeEnabled();
}

function draftOf(page: Page, language: Language = "en") {
    return page.getByRole("region", {
        name: labels[language].draft,
        exact: true,
    });
}

async function editEight(page: Page, language: Language = "en") {
    await page.getByRole("button", { name: labels[language].edit }).click();
    const editor = page.locator(".production-row-editor");
    await editor.getByRole("textbox").first().fill("0,5");
    return {
        editor,
        save: editor.getByRole("button", {
            name: labels[language].save,
            exact: true,
        }),
    };
}

async function stageProposal(page: Page, language: Language = "en") {
    const said = labels[language];
    await page.getByRole("button", { name: said.suggest }).click();
    const editor = page.locator(".production-row-editor");
    await editor.getByRole("textbox").first().fill("0,5");
    await editor.getByRole("button", { name: said.add, exact: true }).click();
    const draft = draftOf(page, language);
    await draft.getByLabel(said.reason).fill("Meter read on site");
    return {
        draft,
        reason: draft.getByLabel(said.reason),
        send: draft.getByRole("button", { name: said.send, exact: true }),
    };
}

const csv = [
    "starts_at,ends_at,energy_kwh,quality,source",
    "2026-11-09T08:00:00+03:00,2026-11-09T08:30:00+03:00,250,valid,meter",
    "2026-11-09T08:30:00+03:00,2026-11-09T09:00:00+03:00,500,valid,meter",
    "2026-11-09T09:00:00+03:00,2026-11-09T09:30:00+03:00,250,valid,meter",
    "2026-11-09T09:30:00+03:00,2026-11-09T10:00:00+03:00,250,valid,meter",
    "2026-11-09T10:00:00+03:00,2026-11-09T10:30:00+03:00,250,valid,meter",
].join("\n");

// 08:00 has no reading, 08:30 has another one the import replaces, 09:00 already has the imported one, and 09:30 and
// 10:00 have its energy with another quality or source, which the import replaces.
const importHeads = {
    "2026-11-09T08:30:00+03:00": 1000,
    "2026-11-09T09:00:00+03:00": 250,
    "2026-11-09T09:30:00+03:00": { energy: 250, quality: "outage" },
    "2026-11-09T10:00:00+03:00": { energy: 250, source: "scada" },
};

async function prepareImport(page: Page, language: Language = "en") {
    const said = labels[language];
    await page
        .getByRole("button", { name: said.importCsv, exact: true })
        .click();
    await page.getByLabel(said.csv).setInputFiles({
        name: "meter.csv",
        mimeType: "text/csv",
        buffer: Buffer.from(csv),
    });
    await page.getByRole("button", { name: said.preview, exact: true }).click();
    return {
        importer: page.locator(".production-import"),
        confirm: page.getByRole("button", { name: said.confirm }),
    };
}

async function reportOutage(page: Page, language: Language = "en") {
    const said = labels[language];
    await page.getByRole("button", { name: said.report, exact: true }).click();
    const form = page.getByRole("region", { name: said.report });
    await form.getByLabel(said.title).fill("Grid fault");
    return {
        form,
        save: form.getByRole("button", { name: said.saveOutage, exact: true }),
    };
}

async function editOutage(page: Page, language: Language = "en") {
    const said = labels[language];
    await page
        .getByRole("button", { name: said.editReport, exact: true })
        .click();
    await page.getByLabel(said.title).fill("Inverter tripped");
    return {
        form: page.locator("form", { has: page.getByLabel(said.title) }),
        save: page.getByRole("button", { name: said.saveChanges, exact: true }),
    };
}

function outageCard(page: Page) {
    return page.locator("article", {
        has: page.getByRole("heading", { name: activeOutage.title }),
    });
}

async function resolveOutage(page: Page, language: Language = "en") {
    const said = labels[language];
    await page.getByRole("button", { name: said.resolve, exact: true }).click();
    const card = outageCard(page);
    return {
        card,
        save: card.getByRole("button", {
            name: said.saveResolution,
            exact: true,
        }),
    };
}

async function cancelOutage(page: Page, language: Language = "en") {
    const said = labels[language];
    const card = outageCard(page);
    await card.getByText(said.cancelEntry, { exact: true }).click();
    await card.getByRole("checkbox", { name: said.confirmCancel }).check();
    return {
        card,
        save: card.getByRole("button", {
            name: said.cancelOutage,
            exact: true,
        }),
    };
}

const outageReads = /\/plants\/21\/outages\/$/;
const productionReads = /\/production\/\?/;

for (const language of ["en", "tr"] as const) {
    const said = labels[language];

    test(`a direct production change lost before and after it was applied is told with the advice and saved again unchanged applies once (${language})`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(now);
        await signIn(page, { language });
        const server = await serveDirectProduction(
            page,
            answers("drop", "cut"),
        );
        await openPlant(page, said.production);
        const { editor, save } = await editEight(page, language);
        for (let send = 0; send < 2; send++) {
            await save.click();
            await expectUnconfirmed(editor, true, language);
            await expect(editor.getByRole("textbox").first()).toHaveValue(
                "0,5",
            );
            await expect(save).toBeEnabled();
        }
        await save.click();
        await expect(editor).toHaveCount(0);
        expect(server.requests).toHaveLength(3);
        expect(server.requests[1]).toEqual(server.requests[0]);
        expect(server.requests[2]).toEqual(server.requests[0]);
        expect(server.applied()).toBe(1);
        expect(server.energy()).toBe(0.5);
    });

    test(`a proposal lost before and after it was applied is told with the advice and sent again with its key stores one request (${language})`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(now);
        await signIn(page, { language, role: "viewer" });
        const server = await serveProposals(page, answers("drop", "cut"));
        await openPlant(page, said.production);
        const { draft, send } = await stageProposal(page, language);
        await send.click();
        await expectUnconfirmed(draft, true, language);
        const release = await holdReads(page, productionReads);
        await send.click();
        await expectUnconfirmed(draft, true, language);
        release();
        await expect(send).toBeDisabled();
        await expectUnconfirmed(draft, false, language);
        expect(server.requests).toHaveLength(2);
        expect(typeof server.requests[0]?.idempotency_key).toBe("string");
        expect(server.requests[1]).toEqual(server.requests[0]);
        expect(server.created()).toBe(1);
    });

    test(`an import lost before and after it was applied is told with the advice and confirmed again shows the server's counts once (${language})`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(now);
        await signIn(page, { language });
        await serveProduction(page, [{ can_edit: true }]);
        const server = await serveImport(
            page,
            answers("drop", "cut"),
            importHeads,
        );
        await openPlant(page, said.production);
        const { importer, confirm } = await prepareImport(page, language);
        for (let send = 0; send < 2; send++) {
            await confirm.click();
            await expectUnconfirmed(importer, true, language);
        }
        await confirm.click();
        await expect(importer.getByText(said.imported)).toBeVisible();
        await expect(confirm).toHaveCount(0);
        expect(server.requests).toHaveLength(3);
        expect(server.requests[1]).toEqual(server.requests[0]);
        expect(server.requests[2]).toEqual(server.requests[0]);
        expect(server.applied()).toBe(4);
    });

    test(`a review decision lost before and after it was applied is told with the advice until the detail shows it decided (${language})`, async ({
        page,
    }) => {
        const { decisions, applied, detail, approve } = await reviewRequest(
            page,
            answers("drop", "cut"),
            language,
        );
        await approve.click();
        await expectUnconfirmed(detail, true, language);
        await expect(approve).toBeEnabled();
        const release = await holdReads(page, /change-requests\/1\/$/);
        await approve.click();
        await expectUnconfirmed(detail, true, language);
        release();
        await expect(approve).toHaveCount(0);
        await expectUnconfirmed(detail, false, language);
        expect(decisions).toEqual([
            { review_fingerprint: "context-1" },
            { review_fingerprint: "context-1" },
        ]);
        expect(applied()).toBe(1);
    });

    test(`an outage report lost before and after it was applied is told with the advice and saved again with its key makes one outage (${language})`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(now);
        await signIn(page, { language });
        const server = await serveOutageWrites(page, answers("drop", "cut"));
        await openPlant(page, said.outages);
        const { form, save } = await reportOutage(page, language);
        for (let send = 0; send < 2; send++) {
            await save.click();
            await expectUnconfirmed(form, true, language);
        }
        await save.click();
        await expect(save).toHaveCount(0);
        await expect(page.locator("h3[data-outage]")).toHaveCount(1);
        await expect(page.getByText(said.sent)).toHaveCount(0);
        const keys = server.writes.map((write) => write.body.idempotency_key);
        expect(keys).toHaveLength(3);
        expect(typeof keys[0]).toBe("string");
        expect(new Set(keys).size).toBe(1);
        expect(server.applied()).toBe(1);
    });

    test(`an outage edit lost before and after it was applied is told with the advice and saved again ends as saved without a third write (${language})`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(now);
        await signIn(page, { language });
        const server = await serveOutageWrites(page, answers("drop", "cut"), [
            activeOutage,
        ]);
        await openPlant(page, said.outages);
        const { form, save } = await editOutage(page, language);
        for (let send = 0; send < 2; send++) {
            await save.click();
            await expectUnconfirmed(form, true, language);
        }
        await save.click();
        await expect(save).toHaveCount(0);
        await expect(
            page.getByRole("heading", { name: "Inverter tripped" }),
        ).toBeVisible();
        expect(
            server.writes.map((write) => write.body.expected_revision),
        ).toEqual([1, 1, 1]);
        expect(server.applied()).toBe(1);
    });

    for (const [name, open] of [
        ["resolution", resolveOutage],
        ["cancellation", cancelOutage],
    ] as const)
        test(`an outage ${name} lost before and after it was applied is told with the advice until the list shows it applied (${language})`, async ({
            page,
        }) => {
            await page.clock.setFixedTime(now);
            await signIn(page, { language });
            const server = await serveOutageWrites(
                page,
                answers("drop", "cut"),
                [activeOutage],
            );
            await openPlant(page, said.outages);
            const { card, save } = await open(page, language);
            await save.click();
            await expectUnconfirmed(card, true, language);
            const release = await holdReads(page, outageReads);
            await save.click();
            await expectUnconfirmed(card, true, language);
            release();
            await expect(save).toHaveCount(0);
            await expectUnconfirmed(card, false, language);
            expect(server.writes).toHaveLength(2);
            expect(server.writes[1]).toEqual(server.writes[0]);
            expect(server.applied()).toBe(1);
        });

    for (const status of [
        "pending",
        "approved",
        "rejected",
        "withdrawn",
    ] as const) {
        const receipt =
            status === "pending"
                ? said.sent
                : said.closed(said.statuses[status]);

        test(`a lost proposal sent again whose request is ${status} by then says so (${language})`, async ({
            page,
        }) => {
            await page.clock.setFixedTime(now);
            await signIn(page, { language, role: "viewer" });
            const server = await serveProposals(page, answers("abort"));
            await openPlant(page, said.production);
            const { draft, send } = await stageProposal(page, language);
            const release = await holdReads(page, productionReads);
            await send.click();
            await expectUnconfirmed(draft, true, language);
            server.decide(status);
            await send.click();
            await expect(
                page.getByText(receipt, { exact: true }),
            ).toBeVisible();
            release();
            expect(server.requests).toHaveLength(2);
            expect(server.requests[1]).toEqual(server.requests[0]);
            expect(server.created()).toBe(1);
        });

        test(`a lost outage proposal sent again whose request is ${status} by then says so (${language})`, async ({
            page,
        }) => {
            await page.clock.setFixedTime(now);
            await signIn(page, { language, role: "viewer" });
            const server = await serveOutageProposals(page, answers("abort"));
            await openPlant(page, said.outages);
            await page
                .getByRole("button", {
                    name: said.proposeResolution,
                    exact: true,
                })
                .click();
            const card = outageCard(page);
            await card.getByLabel(said.optionalReason).fill("Restarted");
            const release = await holdReads(page, outageReads);
            const send = card.getByRole("button", {
                name: labels[language].send,
                exact: true,
            });
            await send.click();
            await expectUnconfirmed(card, true, language);
            server.decide(status);
            await send.click();
            release();
            await expect(
                page.getByText(receipt, { exact: true }),
            ).toBeVisible();
            expect(server.requests).toHaveLength(2);
            expect(server.requests[1]).toEqual(server.requests[0]);
            expect(server.created()).toBe(1);
        });
    }
}

for (const lost of [502, 504, 520, 521, 522, 523, 524, "maintenance"] as const)
    test(`an outage edit answered ${lost === "maintenance" ? "with the maintenance refusal" : `by a bare ${lost}`} is unconfirmed and saved again ends as saved`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(now);
        await signIn(page);
        const server = await serveOutageWrites(page, answers(lost), [
            activeOutage,
        ]);
        await openPlant(page, "Outages");
        const { form, save } = await editOutage(page);
        await save.click();
        await expectUnconfirmed(form, true);
        await save.click();
        await expect(save).toHaveCount(0);
        expect(server.writes).toHaveLength(2);
        expect(server.applied()).toBe(1);
        expect(server.outages()[0]).toMatchObject({
            revision: 2,
            title: "Inverter tripped",
        });
    });

for (const lost of ["drop", "abort"] as const)
    test(`a direct revert lost ${lost === "drop" ? "before" : "after"} it was applied keeps its Revert while the editor would send it, and applies once`, async ({
        page,
    }) => {
        await page.clock.install({ time: now });
        await signIn(page);
        const server = await serveDirectProduction(page, answers(lost));
        server.changeByOperator(0.9);
        await openPlant(page, "Production");
        await page.getByRole("button", { name: /^Edit 08:00/ }).click();
        const editor = page.locator(".production-row-editor");
        const revert = editor.getByRole("button", {
            name: /^Remove entered value for 08:00/,
        });
        await revert.click();
        await expectUnconfirmed(editor, true);
        await readAgainOnFocus(page);
        await expectUnconfirmed(editor, true);
        await expect(revert).toBeEnabled();
        await revert.click();
        await expect(editor).toHaveCount(0);
        expect(server.requests).toHaveLength(2);
        expect(server.requests[1]).toEqual(server.requests[0]);
        expect(server.applied()).toBe(1);
    });

for (const early of ["busy", "throttled"] as const)
    test(`a proposal lost after it was applied whose first repeat is ${early} stores one request when sent again`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(now);
        await signIn(page, { role: "viewer" });
        const refusals: Answering[] =
            early === "busy" ? ["busy", "busy"] : ["throttled"];
        const server = await serveProposals(
            page,
            answers("abort", ...refusals),
        );
        await openPlant(page, "Production");
        const { draft, send } = await stageProposal(page);
        const release = await holdReads(page, productionReads);
        await send.click();
        await expectUnconfirmed(draft, true);
        await send.click();
        await expect(
            draft.getByRole("alert").filter({
                hasText:
                    early === "busy"
                        ? "The session is busy."
                        : "Too many requests.",
            }),
        ).toHaveCount(1);
        await expectUnconfirmed(draft, true);
        await send.click();
        await expect(page.getByText(labels.en.sent)).toBeVisible();
        release();
        expect(server.requests).toHaveLength(2 + refusals.length);
        for (const request of server.requests)
            expect(request).toEqual(server.requests[0]);
        expect(server.created()).toBe(1);
    });

test("an outage edit lost after it was applied and then throttled ends as saved when sent again, without a further write", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveOutageWrites(
        page,
        answers("abort", "throttled"),
        [activeOutage],
    );
    await openPlant(page, "Outages");
    const { form, save } = await editOutage(page);
    await save.click();
    await expectUnconfirmed(form, true);
    await save.click();
    await expect(
        form.getByRole("alert").filter({ hasText: "Too many requests." }),
    ).toHaveCount(1);
    await expectUnconfirmed(form, true);
    await save.click();
    await expect(save).toHaveCount(0);
    expect(server.writes.map((write) => write.body.expected_revision)).toEqual([
        1, 1, 1,
    ]);
    expect(server.applied()).toBe(1);
});

test("an outage edit lost after it was applied that someone changed again meanwhile is not taken as saved", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveOutageWrites(page, answers("abort"), [
        activeOutage,
    ]);
    await openPlant(page, "Outages");
    const { form, save } = await editOutage(page);
    await save.click();
    await expectUnconfirmed(form, true);
    server.changeTitle("Inverter replaced");
    await save.click();
    await expect(
        page.getByText("Someone changed this outage after you opened it."),
    ).toBeVisible();
    await expect(save).toBeVisible();
    expect(server.writes).toHaveLength(2);
    expect(server.applied()).toBe(1);
});

test("a lost proposal, another one refused and the first again send the first one's key", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page, { role: "viewer" });
    const server = await serveProposals(page, answers("drop", "refuse"));
    await openPlant(page, "Production");
    const { draft, reason, send } = await stageProposal(page);
    await send.click();
    await expectUnconfirmed(draft, true);
    await reason.fill("Meter read on site, checked twice");
    await expectUnconfirmed(draft, false);
    await send.click();
    await expect(
        draft.getByRole("alert").filter({ hasText: "The request is invalid." }),
    ).toHaveCount(1);
    await expectUnconfirmed(draft, false);
    await reason.fill("Meter read on site");
    await expectUnconfirmed(draft, true);
    await send.click();
    await expect(page.getByText(labels.en.sent)).toBeVisible();
    const [first, refused, again] = server.requests;
    expect(refused?.idempotency_key).not.toBe(first?.idempotency_key);
    expect(again).toEqual(first);
    expect(server.created()).toBe(1);
});

test("a lost proposal and another one lost make the first one a new request when it is sent again", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page, { role: "viewer" });
    const server = await serveProposals(page, answers("drop", "drop"));
    await openPlant(page, "Production");
    const { draft, reason, send } = await stageProposal(page);
    await send.click();
    await reason.fill("Meter read on site, checked twice");
    await send.click();
    await expectUnconfirmed(draft, true);
    await reason.fill("Meter read on site");
    await expectUnconfirmed(draft, false);
    await send.click();
    await expect(page.getByText(labels.en.sent)).toBeVisible();
    const keys = server.requests.map((request) => request.idempotency_key);
    expect(new Set(keys).size).toBe(3);
    expect({ ...server.requests[2], idempotency_key: null }).toEqual({
        ...server.requests[0],
        idempotency_key: null,
    });
});

test("an edit after a lost direct change is told without the advice and saves a new value against the revision the editor opened with", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveDirectProduction(page, answers("drop"));
    await openPlant(page, "Production");
    const { editor, save } = await editEight(page);
    await save.click();
    await expectUnconfirmed(editor, true);
    await editor.getByRole("textbox").first().fill("0,6");
    await expectUnconfirmed(editor, false);
    await editor.getByRole("textbox").first().fill("0,5");
    await expectUnconfirmed(editor, true);
    await editor.getByRole("textbox").first().fill("0,6");
    await save.click();
    await expect(editor).toHaveCount(0);
    expect(server.requests).toHaveLength(2);
    expect(server.requests[1]).toMatchObject({
        changes: [{ energy: 0.6, expected_revision_id: null }],
    });
    expect(server.energy()).toBe(0.6);
});

test("a member's lost resolution is told without the advice after a promotion, and its form then resolves directly", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    const session = await signIn(page, { role: "viewer" });
    let direct = false;
    const server = await serveOutageProposals(
        page,
        answers("drop"),
        () => direct,
    );
    await openPlant(page, "Outages");
    await page
        .getByRole("button", { name: "Submit resolution", exact: true })
        .click();
    const card = outageCard(page);
    const send = card.getByRole("button", {
        name: "Send for approval",
        exact: true,
    });
    await send.click();
    await expectUnconfirmed(card, true);
    await outageReadsAnswered(page);
    session.changeRole("admin");
    direct = true;
    await readAgainOnFocus(page);
    const save = card.getByRole("button", {
        name: "Save resolution",
        exact: true,
    });
    await expect(save).toBeEnabled();
    await expectUnconfirmed(card, false);
    await save.click();
    await expect.poll(() => server.direct.length).toBe(1);
    expect(server.direct[0]?.url).toBe(
        "/api/tenants/1/regions/11/plants/21/outages/51/resolve/",
    );
    expect(server.direct[0]?.body).not.toHaveProperty("idempotency_key");
    expect(server.requests).toHaveLength(1);
});

test("a lost approval is told without the advice once the review changed, and Approve sends the new fingerprint", async ({
    page,
}) => {
    const { request, decisions, detail, approve, refresh } =
        await reviewRequest(page, answers("drop"));
    await approve.click();
    await expectUnconfirmed(detail, true);
    request.review_fingerprint = "context-2";
    await refresh.click();
    await expectUnconfirmed(detail, false);
    await expect(approve).toBeEnabled();
    await approve.click();
    await expect(approve).toHaveCount(0);
    expect(decisions).toEqual([
        { review_fingerprint: "context-1" },
        { review_fingerprint: "context-2" },
    ]);
});

test("a lost outage edit is told without the advice once the plant's time zone changed, and saving sends the times of the new zone", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    await signIn(page);
    let timezone = region.timezone;
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.request().method() === "GET"
            ? route.fulfill({
                  json: paged([{ ...region, timezone, plant, reminder: null }]),
              })
            : route.fallback(),
    );
    const server = await serveOutageWrites(page, answers("drop"), [
        activeOutage,
    ]);
    await openPlant(page, "Outages");
    const { form, save } = await editOutage(page);
    await save.click();
    await expectUnconfirmed(form, true);
    await outageReadsAnswered(page);
    timezone = "Europe/London";
    await readAgainOnFocus(page);
    await expectUnconfirmed(form, false);
    await save.click();
    await expect(save).toHaveCount(0);
    expect(server.writes.map((write) => write.body.starts_at)).toEqual([
        "2026-11-09T08:00:00.000Z",
        "2026-11-09T11:00:00.000Z",
    ]);
});

test("a repeat keeps the proposal's inputs disabled while it runs, and its answer ends it", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page, { role: "viewer" });
    const server = await serveProposals(page, answers("drop"));
    const writes = await holdWrites(page, "**/change-requests/production/");
    await openPlant(page, "Production");
    const { draft, reason, send } = await stageProposal(page);
    await send.click();
    await expectUnconfirmed(draft, true);
    writes.hold();
    await send.click();
    await expect(reason).toBeDisabled();
    await expect(
        draft.getByRole("button", { name: "Sending…" }),
    ).toBeDisabled();
    writes.release();
    await expect(page.getByText(labels.en.sent)).toBeVisible();
    expect(server.created()).toBe(1);
});

test("a repeat keeps the outage form disabled while it runs, and its refusal ends it with the form usable", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers("drop", "refuse"), [activeOutage]);
    const writes = await holdWrites(page, "**/plants/21/outages/**");
    await openPlant(page, "Outages");
    const { form, save } = await editOutage(page);
    await save.click();
    await expectUnconfirmed(form, true);
    writes.hold();
    await save.click();
    await expect(page.getByLabel("Incident title")).toBeDisabled();
    writes.release();
    await expect(form.getByRole("alert")).toContainText(
        "The request is invalid.",
    );
    await expect(page.getByLabel("Incident title")).toBeEnabled();
    await expect(save).toBeEnabled();
});

test("a lost proposal that arrived is held by the read that shows its pending request, without Send or the advice", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page, { role: "viewer" });
    const server = await serveProposals(page, answers("abort"));
    await openPlant(page, "Production");
    const { draft, send } = await stageProposal(page);
    await send.click();
    await expect(draft).toContainText(
        "Intervals that already wait for a request of yours cannot be sent again.",
    );
    await expect(send).toBeDisabled();
    await expectUnconfirmed(draft, false);
    expect(server.requests).toHaveLength(1);
});

test("a member's lost outage proposal that arrived is held by the read that shows its pending request, without the advice", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page, { role: "viewer" });
    const server = await serveOutageProposals(page, answers("abort"));
    await openPlant(page, "Outages");
    await page
        .getByRole("button", { name: "Submit resolution", exact: true })
        .click();
    const card = outageCard(page);
    await card
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await expect(card).toContainText(
        "A request of yours for this outage now waits for approval.",
    );
    await expectUnconfirmed(card, false);
    expect(server.requests).toHaveLength(1);
});

test("Refresh details on an unchanged detail leaves Approve enabled", async ({
    page,
}) => {
    const { decisions, approve, refresh } = await reviewRequest(
        page,
        answers(),
    );
    const read = page.waitForResponse((response) =>
        response.url().endsWith("/change-requests/1/"),
    );
    await refresh.click();
    await read;
    await expect(approve).toBeEnabled();
    await approve.click();
    await expect(approve).toHaveCount(0);
    expect(decisions).toHaveLength(1);
});

test("a Refresh details started before a refused approval does not release the hold the refusal set", async ({
    page,
}) => {
    const { request, decisions, detail, approve, refresh } =
        await reviewRequest(page, answers());
    let release = () => {};
    const released = new Promise<void>((resolve) => {
        release = resolve;
    });
    let took = () => {};
    const taken = new Promise<void>((resolve) => {
        took = resolve;
    });
    const earlier = "Verified meter reading, as read before the refusal";
    await page.route(/change-requests\/1\/$/, async (route) => {
        const answered = { ...request, reason: earlier };
        took();
        await released;
        return route.fulfill({ json: answered });
    });
    await refresh.click();
    await taken;
    request.review_fingerprint = "context-2";
    await approve.click();
    await expect(detail).toContainText(
        "The submission changed. Review it again.",
    );
    await expect(approve).toBeDisabled();
    release();
    await expect(detail).toContainText(earlier);
    await expect(detail).toContainText(
        "The current data changed. Refresh and review it again before approving.",
    );
    await expect(approve).toBeDisabled();
    expect(decisions).toEqual([{ review_fingerprint: "context-1" }]);
});

const unreachable =
    "Could not reach the server. Check your connection and try again.";

test("a CSV preview whose answer was lost says the preview failed and Try again shows it", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveProduction(page, [{ can_edit: true }]);
    await serveImport(page, answers(), importHeads);
    let previews = 0;
    await page.route("**/production/preview/", (route) =>
        previews++ === 0 ? route.abort("failed") : route.fallback(),
    );
    await openPlant(page, "Production");
    const { importer, confirm } = await prepareImport(page);
    const failure = importer.getByRole("alert");
    await expect(failure).toContainText(unreachable);
    await expect(importer).not.toContainText(texts.en.title);
    await failure
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await expect(importer.getByRole("alert")).toHaveCount(0);
    await expect(confirm).toBeVisible();
    expect(previews).toBe(2);
});

test("an invitation preview whose answer was lost says it could not be read and Retry shows it", async ({
    page,
}) => {
    await mockApi(page);
    let previews = 0;
    await page.route("**/api/invitations/preview/", (route) =>
        previews++ === 0 ? route.abort("failed") : route.fallback(),
    );
    await page.goto("/app/invitation/#token=invite-secret");
    const invitation = page.getByRole("main");
    const failure = invitation.getByRole("alert");
    await expect(failure).toContainText(unreachable);
    await expect(invitation).not.toContainText(texts.en.title);
    await failure
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await expect(invitation).toContainText("Sunfield Energy");
    await expect(failure).toHaveCount(0);
    expect(previews).toBe(2);
});

async function proposeOutage(page: Page) {
    await page
        .getByRole("button", { name: "Submit outage", exact: true })
        .click();
    const form = page.getByRole("region", { name: "Report outage" });
    await form.getByLabel("Incident title").fill("Grid fault");
    const reason = form.getByLabel("Reason (optional)");
    await reason.fill("Meter checked");
    return {
        form,
        reason,
        send: form.getByRole("button", {
            name: "Send for approval",
            exact: true,
        }),
    };
}

test("a member's lost outage proposal is told without the advice once a promotion and a demotion emptied its reason, and sent with that reason again stores one proposal", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    const session = await signIn(page, { role: "viewer" });
    let direct = false;
    const server = await serveOutageProposals(
        page,
        answers("abort"),
        () => direct,
    );
    await openPlant(page, "Outages");
    const { form, reason, send } = await proposeOutage(page);
    await send.click();
    await expectUnconfirmed(form, true);
    await outageReadsAnswered(page);
    session.changeRole("admin");
    direct = true;
    await readAgainOnFocus(page);
    await expect(
        form.getByRole("button", { name: "Save outage", exact: true }),
    ).toBeVisible();
    session.changeRole("viewer");
    direct = false;
    await readAgainOnFocus(page);
    await expect(send).toBeVisible();
    await expect(reason).toHaveValue("");
    await expectUnconfirmed(form, false);
    await reason.fill("Meter checked");
    await expectUnconfirmed(form, true);
    await send.click();
    await expect(page.getByText(labels.en.sent)).toBeVisible();
    expect(server.requests).toHaveLength(2);
    expect(server.requests[1]).toEqual(server.requests[0]);
    expect(server.created()).toBe(1);
});

// Outer whitespace the server strips from a text: ordinary spaces, and characters a browser's trim keeps.
const outerSpace = {
    "outer spaces": (text: string) => ` ${text}  `,
    "a trailing next line": (text: string) => `${text}\u0085`,
    "outer separators": (text: string) => `\u001f${text}\u001c`,
};

for (const [name, padded] of Object.entries(outerSpace)) {
    test(`a lost outage proposal whose reason only gained ${name} is sent again with its key and stores one proposal`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(now);
        await signIn(page, { role: "viewer" });
        const server = await serveOutageProposals(page, answers("abort"));
        await openPlant(page, "Outages");
        const { form, reason, send } = await proposeOutage(page);
        await send.click();
        await expectUnconfirmed(form, true);
        await reason.fill(padded("Meter checked"));
        await expectUnconfirmed(form, true);
        await send.click();
        await expect(page.getByText(labels.en.sent)).toBeVisible();
        expect(server.requests).toHaveLength(2);
        expect(server.requests[1]).toEqual(server.requests[0]);
        expect(server.requests[0]?.reason).toBe("Meter checked");
        expect(server.created()).toBe(1);
    });

    test(`a lost outage report whose title only gained ${name} is saved again with its key and makes one outage`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(now);
        await signIn(page);
        const server = await serveOutageWrites(page, answers("abort"));
        await openPlant(page, "Outages");
        const { form, save } = await reportOutage(page);
        await save.click();
        await expectUnconfirmed(form, true);
        await form.getByLabel(labels.en.title).fill(padded("Grid fault"));
        await expectUnconfirmed(form, true);
        await save.click();
        await expect(save).toHaveCount(0);
        expect(server.writes).toHaveLength(2);
        expect(server.writes[1]).toEqual(server.writes[0]);
        expect(server.writes[0]?.body.title).toBe("Grid fault");
        expect(server.applied()).toBe(1);
    });

    test(`a lost proposal whose reason only gained ${name} is sent again with its key and stores one request`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(now);
        await signIn(page, { role: "viewer" });
        const server = await serveProposals(page, answers("drop"));
        await openPlant(page, "Production");
        const { draft, reason, send } = await stageProposal(page);
        await send.click();
        await expectUnconfirmed(draft, true);
        await reason.fill(padded("Meter read on site"));
        await expectUnconfirmed(draft, true);
        await send.click();
        await expect(page.getByText(labels.en.sent)).toBeVisible();
        expect(server.requests).toHaveLength(2);
        expect(server.requests[1]).toEqual(server.requests[0]);
        expect(server.created()).toBe(1);
    });
}

test("a lost outage proposal is told without the advice once the plant's configuration changed, and Send stores a new proposal under it", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    await signIn(page, { role: "viewer" });
    let configuration = plant.current_configuration;
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.request().method() === "GET"
            ? route.fulfill({
                  json: paged([
                      {
                          ...region,
                          plant: {
                              ...plant,
                              current_configuration: configuration,
                          },
                          reminder: null,
                      },
                  ]),
              })
            : route.fallback(),
    );
    const server = await serveOutageProposals(
        page,
        answers("abort"),
        () => false,
        () => configuration.id,
    );
    await openPlant(page, "Outages");
    const { form, send } = await proposeOutage(page);
    await send.click();
    await expectUnconfirmed(form, true);
    await outageReadsAnswered(page);
    configuration = { ...configuration, id: 32, revision: 2 };
    await readAgainOnFocus(page);
    await expectUnconfirmed(form, false);
    await send.click();
    await expect(page.getByText(labels.en.sent)).toBeVisible();
    const [first, second] = server.requests;
    expect(second?.idempotency_key).not.toBe(first?.idempotency_key);
    expect(server.stored().map((request) => request.configuration)).toEqual([
        31, 32,
    ]);
});

test("an outage edit lost after it was applied, another edit refused as stale and the first edit restored ends as saved without another revision", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveOutageWrites(page, answers("abort"), [
        activeOutage,
    ]);
    await openPlant(page, "Outages");
    const { form, save } = await editOutage(page);
    await save.click();
    await expectUnconfirmed(form, true);
    const title = page.getByLabel("Incident title");
    await title.fill("Inverter replaced");
    await save.click();
    await expect(
        page.getByText("Someone changed this outage after you opened it."),
    ).toBeVisible();
    await expectUnconfirmed(form, false);
    await title.fill("Inverter tripped");
    await expectUnconfirmed(form, true);
    await save.click();
    await expect(save).toHaveCount(0);
    expect(server.writes.map((write) => write.body.expected_revision)).toEqual([
        1, 1, 1,
    ]);
    expect(server.applied()).toBe(1);
    expect(server.outages()[0]).toMatchObject({
        revision: 2,
        title: "Inverter tripped",
    });
});

test("unchecking the confirmation of a lost cancellation removes the advice without sending, and checking it brings the advice back", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveOutageWrites(page, answers("drop"), [
        activeOutage,
    ]);
    await openPlant(page, "Outages");
    const { card, save } = await cancelOutage(page);
    await save.click();
    await expectUnconfirmed(card, true);
    const confirmation = card.getByRole("checkbox", {
        name: labels.en.confirmCancel,
    });
    await confirmation.uncheck();
    await expectUnconfirmed(card, false);
    await confirmation.check();
    await expectUnconfirmed(card, true);
    expect(server.writes).toHaveLength(1);
    await save.click();
    await expect(save).toHaveCount(0);
    expect(server.writes).toHaveLength(2);
    expect(server.writes[1]).toEqual(server.writes[0]);
    expect(server.applied()).toBe(1);
});

test("unchecking the capacity confirmation of a lost import removes the advice without sending, and the import confirmed again applies once", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveProduction(page, [{ can_edit: true }]);
    const server = await serveImport(page, answers("abort"), importHeads, [
        {
            code: "energy_exceeds_capacity",
            starts_at: "2026-11-09T08:30:00+03:00",
            ends_at: "2026-11-09T09:00:00+03:00",
        },
    ]);
    await openPlant(page, "Production");
    const { importer, confirm } = await prepareImport(page);
    const confirmation = importer.getByRole("checkbox", {
        name: "I checked these values and want to save them.",
    });
    await confirmation.check();
    await confirm.click();
    await expectUnconfirmed(importer, true);
    await confirmation.uncheck();
    await expectUnconfirmed(importer, false);
    await confirmation.check();
    await expectUnconfirmed(importer, true);
    expect(server.requests).toHaveLength(1);
    await confirm.click();
    await expect(importer.getByText(labels.en.imported)).toBeVisible();
    expect(server.requests).toHaveLength(2);
    expect(server.requests[1]).toEqual(server.requests[0]);
    expect(server.requests[0]).toMatchObject({ confirm_implausible: true });
    expect(server.applied()).toBe(4);
});

test("a direct change lost after it took its interval out of the default view keeps the editor and its advice in view", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveDirectProduction(page, answers("cut"), [
        { can_edit: true },
        { can_edit: true },
    ]);
    await openPlant(page, "Production");
    await page.getByRole("button", { name: /^Edit 08:00/ }).click();
    const editor = page.locator(".production-row-editor");
    const energy = editor.getByRole("textbox").first();
    await energy.fill("0");
    const save = editor.getByRole("button", { name: "Save", exact: true });
    const reread = page.waitForResponse(
        (response) =>
            productionReads.test(response.url()) && server.requests.length > 0,
    );
    await save.click();
    await reread;
    await expectUnconfirmed(editor, true);
    await expect(energy).toHaveValue("0");
    await save.click();
    await expect(editor).toHaveCount(0);
    expect(server.requests).toHaveLength(2);
    expect(server.requests[1]).toEqual(server.requests[0]);
    expect(server.applied()).toBe(1);
    expect(server.energy()).toBe(0);
});

test("a revert lost after it merged its half hour into an hourly gap keeps its unknown outcome and is sent again once", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveHalfHourCorrection(page, answers("abort"));
    await openPlant(page, "Production");
    await page.getByRole("button", { name: /^Edit 08:00/ }).click();
    await page
        .getByRole("button", { name: /^Remove entered value for 08:00/ })
        .click();
    const orphan = page.locator(".production-row-editor");
    await expect(orphan).toContainText(
        "This interval changed and is no longer available.",
    );
    await expectUnconfirmed(orphan, true);
    await orphan
        .getByRole("button", { name: "Remove entered value", exact: true })
        .click();
    await expect(orphan).toHaveCount(0);
    await expect(page.getByText("Entered value removed.")).toBeVisible();
    expect(server.requests).toHaveLength(2);
    expect(server.requests[1]).toEqual(server.requests[0]);
    expect(server.applied()).toBe(1);
});

test("Reload current values after a lost direct change keeps its notice and sends it against the revision it was sent with", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveDirectProduction(page, answers("drop"));
    await openPlant(page, "Production");
    const { editor, save } = await editEight(page);
    await save.click();
    await expectUnconfirmed(editor, true);
    server.changeByOperator(0.9);
    const reread = page.waitForResponse((response) =>
        productionReads.test(response.url()),
    );
    await editor
        .getByRole("button", { name: "Reload current values", exact: true })
        .click();
    await reread;
    await expectUnconfirmed(editor, true);
    await save.click();
    await expect(editor.getByRole("alert")).toContainText(
        "Production changed. Reload the current values and review them.",
    );
    expect(server.requests).toHaveLength(2);
    expect(server.requests[1]).toEqual(server.requests[0]);
    expect(server.applied()).toBe(0);
    expect(server.energy()).toBe(0.9);
});

test("a lost direct change whose repeat is throttled keeps Save enabled and applies once", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveDirectProduction(
        page,
        answers("abort", "throttled"),
    );
    await openPlant(page, "Production");
    const { editor, save } = await editEight(page);
    await save.click();
    await expectUnconfirmed(editor, true);
    await save.click();
    await expect(
        editor.getByRole("alert").filter({ hasText: "Too many requests." }),
    ).toHaveCount(1);
    await expectUnconfirmed(editor, true);
    await expect(save).toBeEnabled();
    await save.click();
    await expect(editor).toHaveCount(0);
    expect(server.requests).toHaveLength(3);
    for (const request of server.requests)
        expect(request).toEqual(server.requests[0]);
    expect(server.applied()).toBe(1);
});

test("a direct change lost after it was applied is read again by a read started after it, not by one already running", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveDirectProduction(page, answers("abort"));
    await openPlant(page, "Production");
    const reading = page.locator(".production-value").first();
    await expect(reading).toContainText("1");
    let release = () => {};
    const released = new Promise<void>((resolve) => {
        release = resolve;
    });
    let held = false;
    let readsAfterWrite = 0;
    await page.route(productionReads, async (route) => {
        if (server.requests.length) {
            readsAfterWrite++;
            return route.fallback();
        }
        if (held) return route.fallback();
        held = true;
        const before = server.day(
            new URL(route.request().url()).searchParams.get("start_date")!,
        );
        await released;
        return route.fulfill({ json: before });
    });
    const refreshing = page.waitForRequest(productionReads);
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await refreshing;
    const { editor, save } = await editEight(page);
    await save.click();
    await expectUnconfirmed(editor, true);
    await expect(reading).toContainText("0.5");
    expect(readsAfterWrite).toBeGreaterThan(0);
    release();
});

test("a decision lost after it was applied is read again by a read started after it, not by a Refresh details already running", async ({
    page,
}) => {
    const { request, applied, detail, approve, refresh } = await reviewRequest(
        page,
        answers("abort"),
    );
    let release = () => {};
    const released = new Promise<void>((resolve) => {
        release = resolve;
    });
    let took = () => {};
    const taken = new Promise<void>((resolve) => {
        took = resolve;
    });
    let held = false;
    let readsAfterDecision = 0;
    await page.route(/change-requests\/1\/$/, async (route) => {
        if (held) {
            if (request.status !== "pending") readsAfterDecision++;
            return route.fallback();
        }
        held = true;
        const answered = { ...request };
        took();
        await released;
        return route.fulfill({ json: answered });
    });
    await refresh.click();
    await taken;
    await approve.click();
    await expect(approve).toHaveCount(0);
    await expectUnconfirmed(detail, false);
    expect(readsAfterDecision).toBeGreaterThan(0);
    expect(applied()).toBe(1);
    release();
});

test("Refresh details keeps the notice of a lost approval when the detail is unchanged and when its read fails", async ({
    page,
}) => {
    const { applied, detail, approve, refresh } = await reviewRequest(
        page,
        answers("drop"),
    );
    await approve.click();
    await expectUnconfirmed(detail, true);
    const read = page.waitForResponse((response) =>
        response.url().endsWith("/change-requests/1/"),
    );
    await refresh.click();
    await read;
    await expectUnconfirmed(detail, true);
    await expect(approve).toBeEnabled();
    await page.route(/change-requests\/1\/$/, (route) =>
        route.fulfill({
            status: 500,
            json: errorBody("server_error", "The server failed."),
        }),
    );
    await refresh.click();
    await expect(detail).toContainText("The server failed.");
    await expectUnconfirmed(detail, false);
    expect(applied()).toBe(0);
});

test("a lost rejection is told once by its form, follows its reason, and is told by the detail once the request was decided", async ({
    page,
}) => {
    const { request, applied, detail, refresh } = await reviewRequest(
        page,
        answers("drop"),
    );
    const reason = detail.getByLabel("Reason for rejection");
    const reject = detail.getByRole("button", { name: "Reject", exact: true });
    await reason.fill("The meter reading is wrong");
    await reject.click();
    await expectUnconfirmed(detail, true);
    await reason.fill("The meter reading is wrong.");
    await expectUnconfirmed(detail, false);
    await reason.fill("The meter reading is wrong\u0085");
    await expectUnconfirmed(detail, true);
    Object.assign(request, { status: "approved", can_review: false });
    await refresh.click();
    await expect(reject).toHaveCount(0);
    await expectUnconfirmed(detail, false);
    expect(applied()).toBe(0);
});

for (const language of ["en", "tr"] as const)
    test(`a plant whose creation answer was lost is told as not confirmed, also after the language changed (${language})`, async ({
        page,
    }) => {
        const account = await mockApi(page, { empty: true });
        await page.route("**/api/session/language/", (route) => {
            const chosen = route.request().postDataJSON().language as string;
            account.current = accountWith({ language_preference: chosen });
            return route.fulfill({
                json: { ...sessionIdentity(account.current), language: chosen },
            });
        });
        await page.route("**/sites/", (route) =>
            route.request().method() === "POST"
                ? route.abort("failed")
                : route.fallback(),
        );
        await page.goto("/app/");
        await page.getByRole("button", { name: "Add a plant" }).click();
        await page.getByLabel("Plant name", { exact: true }).fill("West plant");
        await page.getByLabel("Latitude").fill("37");
        await page.getByLabel("Longitude").fill("31");
        await page
            .getByRole("button", { name: "Continue", exact: true })
            .click();
        await page.getByLabel("DC nameplate capacity (MW)").fill("5");
        await page
            .getByRole("button", { name: "Continue", exact: true })
            .click();
        await page
            .getByRole("button", { name: "Submit plant for approval" })
            .click();
        const main = page.locator("main");
        await expectUnconfirmed(main, false);
        if (language === "tr") {
            await page
                .getByLabel("Language", { exact: true })
                .selectOption("tr");
            await expect(page.locator("html")).toHaveAttribute("lang", "tr");
            await expectUnconfirmed(main, false, "tr");
        }
    });

test("a reminder preview whose answer was lost says the request failed, not that a change may have been saved", async ({
    page,
}) => {
    await reminderApi(page, { enabled: true });
    let previews = 0;
    await page.route("**/reminders/preview/", (route) => {
        previews++;
        return route.abort("failed");
    });
    await openReminders(page);
    await page
        .getByLabel("Hide recipients from each other", { exact: true })
        .uncheck();
    await page.getByRole("button", { name: "Save reminders" }).click();
    const main = page.locator("main");
    await expect(main.getByText(unreachable)).toBeVisible();
    await expect(main).not.toContainText(texts.en.title);
    await expect(main).not.toContainText(texts.en.general);
    expect(previews).toBe(1);
});

test("an edit of a resolved outage lost after it was applied is advised and saved again with its revision after the plant's configuration changed, without a further revision", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    await signIn(page);
    let configuration = plant.current_configuration;
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.request().method() === "GET"
            ? route.fulfill({
                  json: paged([
                      {
                          ...region,
                          plant: {
                              ...plant,
                              current_configuration: configuration,
                          },
                          reminder: null,
                      },
                  ]),
              })
            : route.fallback(),
    );
    const server = await serveOutageWrites(page, answers("abort"), [
        {
            ...activeOutage,
            status: "resolved",
            ends_at: "2026-11-09T10:00:00Z",
        },
    ]);
    await openPlant(page, "Outages");
    const { form, save } = await editOutage(page);
    await save.click();
    await expectUnconfirmed(form, true);
    await outageReadsAnswered(page);
    configuration = { ...configuration, id: 32, revision: 2 };
    const reread = page.waitForResponse((response) =>
        response.url().endsWith("/api/tenants/1/sites/"),
    );
    await readAgainOnFocus(page);
    await reread;
    await expectUnconfirmed(form, true);
    await save.click();
    await expect(save).toHaveCount(0);
    expect(server.writes.map((write) => write.body.expected_revision)).toEqual([
        1, 1,
    ]);
    expect(server.applied()).toBe(1);
    expect(server.outages()[0]).toMatchObject({
        revision: 2,
        title: "Inverter tripped",
    });
});

test("an orphaned direct change names the write its repeat sends while it shows a newer entry, and sends that write", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveSplitSlot(page, answers("drop"));
    await openPlant(page, "Production");
    const { editor, save } = await editEight(page);
    const release = await holdReads(page, productionReads);
    await save.click();
    await expectUnconfirmed(editor, true);
    await editor.getByRole("textbox").first().fill("1,50");
    await expectUnconfirmed(editor, false);
    server.merge();
    release();
    const orphan = page.locator(".production-row-editor");
    await expect(orphan).toContainText(
        "This interval changed and is no longer available.",
    );
    await expect(orphan.getByRole("textbox")).toHaveValue("1,50");
    await expectUnconfirmed(orphan, false);
    await orphan
        .getByRole("button", { name: "Save 0.5 MWh (Valid)", exact: true })
        .click();
    await expect(orphan).toHaveCount(0);
    expect(server.requests).toHaveLength(2);
    expect(server.requests[1]).toEqual(server.requests[0]);
    expect(server.applied()).toBe(1);
});

test("Reload current values that finds the interval's bounds changed keeps the lost change's notice, and its repeat applies it once", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveSplitSlot(page, answers("drop"));
    await openPlant(page, "Production");
    const { editor, save } = await editEight(page);
    const reread = page.waitForResponse((response) =>
        productionReads.test(response.url()),
    );
    await save.click();
    await reread;
    await expectUnconfirmed(editor, true);
    server.merge();
    await editor
        .getByRole("button", { name: "Reload current values", exact: true })
        .click();
    const orphan = page.locator(".production-row-editor");
    await expect(orphan).toContainText(
        "This interval changed and is no longer available.",
    );
    await expectUnconfirmed(orphan, true);
    await orphan
        .getByRole("button", { name: "Save 0.5 MWh (Valid)", exact: true })
        .click();
    await expect(orphan).toHaveCount(0);
    expect(server.requests).toHaveLength(2);
    expect(server.requests[1]).toEqual(server.requests[0]);
    expect(server.applied()).toBe(1);
});

test("a failed Reload current values after a lost direct change is told beside its notice, and a later reload keeps the notice alone", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveDirectProduction(page, answers("drop"));
    await openPlant(page, "Production");
    const { editor, save } = await editEight(page);
    const reread = page.waitForResponse((response) =>
        productionReads.test(response.url()),
    );
    await save.click();
    await reread;
    await expectUnconfirmed(editor, true);
    const failing = (route: Route) =>
        route.fulfill({
            status: 500,
            json: errorBody("server_error", "The server failed."),
        });
    await page.route(productionReads, failing);
    const reload = editor.getByRole("button", {
        name: "Reload current values",
        exact: true,
    });
    await reload.click();
    await expect(editor).toContainText("The server failed.");
    await expectUnconfirmed(editor, true);
    await page.unroute(productionReads, failing);
    await reload.click();
    await expect(editor).not.toContainText("The server failed.");
    await expectUnconfirmed(editor, true);
    await save.click();
    await expect(editor).toHaveCount(0);
    expect(server.requests).toHaveLength(2);
    expect(server.requests[1]).toEqual(server.requests[0]);
    expect(server.applied()).toBe(1);
});

test("an orphaned direct change whose outcome is unknown keeps its notice once the person can only propose, without sending it", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    const session = await signIn(page);
    const server = await serveSplitSlot(page, answers("drop"));
    await openPlant(page, "Production");
    const { editor, save } = await editEight(page);
    const reread = page.waitForResponse((response) =>
        productionReads.test(response.url()),
    );
    await save.click();
    await reread;
    server.merge();
    await editor
        .getByRole("button", { name: "Reload current values", exact: true })
        .click();
    const orphan = page.locator(".production-row-editor");
    const repeat = orphan.getByRole("button", {
        name: "Save 0.5 MWh (Valid)",
        exact: true,
    });
    await expect(repeat).toBeVisible();
    await expectUnconfirmed(orphan, true);
    session.changeRole("viewer");
    await readAgainOnFocus(page);
    await expect(repeat).toHaveCount(0);
    await expect(orphan).toContainText(
        "This interval changed and is no longer available.",
    );
    await expectUnconfirmed(orphan, false);
    expect(server.requests).toHaveLength(1);
});

test("a lost rejection keeps its reason and its notice while its request cannot be read, and Reject then applies it once", async ({
    page,
}) => {
    const { applied, detail, refresh } = await reviewRequest(
        page,
        answers("drop"),
    );
    const reason = detail.getByLabel("Reason for rejection");
    const reject = detail.getByRole("button", { name: "Reject", exact: true });
    await reason.fill("The meter reading is wrong");
    await reject.click();
    await expectUnconfirmed(detail, true);
    let gone = true;
    await page.route(/change-requests\/1\/$/, (route) =>
        gone
            ? route.fulfill({
                  status: 404,
                  json: errorBody("not_found", "Not found."),
              })
            : route.fallback(),
    );
    await refresh.click();
    await expect(detail).toContainText("Not found.");
    await expect(reason).toHaveCount(0);
    await expectUnconfirmed(detail, false);
    gone = false;
    await refresh.click();
    await expect(reason).toHaveValue("The meter reading is wrong");
    await expectUnconfirmed(detail, true);
    await reject.click();
    await expect(reject).toHaveCount(0);
    expect(applied()).toBe(1);
});

test("a lost approval whose repeat is throttled keeps its notice beside the throttle and through Refresh details, and Approve applies it once", async ({
    page,
}) => {
    const { decisions, applied, detail, approve, refresh } =
        await reviewRequest(page, answers("drop", "throttled"));
    await approve.click();
    await expectUnconfirmed(detail, true);
    await approve.click();
    await expect(
        detail.getByRole("alert").filter({ hasText: "Too many requests." }),
    ).toHaveCount(1);
    await expectUnconfirmed(detail, true);
    const read = page.waitForResponse((response) =>
        response.url().endsWith("/change-requests/1/"),
    );
    await refresh.click();
    await read;
    await expect(detail).not.toContainText("Too many requests.");
    await expectUnconfirmed(detail, true);
    await approve.click();
    await expect(approve).toHaveCount(0);
    expect(decisions).toHaveLength(3);
    expect(applied()).toBe(1);
});

// Holds every script that reads the texts of `namespace` until `release`.
async function holdScript(page: Page, namespace: string) {
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/_next/static/**/*.js", async (route) => {
        const response = await route.fetch();
        const body = await response.text();
        if (body.includes(`("${namespace}")`)) {
            reach();
            await released;
        }
        await route.fulfill({ response, body });
    });
    return { reached, release: () => release() };
}

test("a lost import whose Confirm a view change on its way disables is told without the advice", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveProduction(page, [{ can_edit: true }]);
    const server = await serveImport(page, answers("drop"), importHeads);
    await openPlant(page, "Production");
    const { importer, confirm } = await prepareImport(page);
    await confirm.click();
    await expectUnconfirmed(importer, true);
    const comparison = await holdScript(page, "ForecastPerformance");
    await page.getByRole("button", { name: "Comparison", exact: true }).click();
    await comparison.reached;
    await expect(confirm).toBeDisabled();
    await expectUnconfirmed(importer, false);
    comparison.release();
    await expect(
        page.getByRole("heading", { name: "Comparison", exact: true }),
    ).toBeVisible();
    expect(server.requests).toHaveLength(1);
});

test("a lost outage proposal whose reason changed without an input event is sent as a new proposal, told without the advice while it runs", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page, { role: "viewer" });
    const server = await serveOutageProposals(page, answers("drop"));
    const writes = await holdWrites(page, "**/change-requests/incidents/");
    await openPlant(page, "Outages");
    const { form, reason, send } = await proposeOutage(page);
    await send.click();
    await expectUnconfirmed(form, true);
    await reason.evaluate((field: HTMLTextAreaElement) => {
        field.value = "Meter replaced";
    });
    writes.hold();
    await send.click();
    await expectUnconfirmed(form, false);
    writes.release();
    await expect(page.getByText(labels.en.sent)).toBeVisible();
    const [first, second] = server.requests;
    expect(second?.idempotency_key).not.toBe(first?.idempotency_key);
    expect(second).toMatchObject({ reason: "Meter replaced" });
    expect(server.created()).toBe(1);
});
