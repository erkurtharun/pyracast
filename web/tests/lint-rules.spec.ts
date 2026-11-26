import { readFileSync } from "node:fs";
import path from "node:path";
import { ESLint } from "eslint";
import { expect, test } from "@playwright/test";

const web = path.resolve();
const eslint = new ESLint({ cwd: web });

// Type-aware rules parse through the TypeScript project, so each probe lints an existing file.
// Building that program takes about 20 s once per worker: the probes share one worker and
// build it before they run, with a budget sized for the build.
test.describe.configure({ mode: "default" });
test.beforeAll(async () => {
    test.setTimeout(120_000);
    await eslint.lintFiles(["src/lib/routes.ts"]);
});

async function rules(file: string, source: string) {
    const [result] = await eslint.lintText(source, {
        filePath: path.join(web, file),
    });
    return result!.messages.map((message) => message.ruleId).sort();
}

const promises = [
    "// eslint-disable",
    "export function probe(save: () => Promise<void>) {",
    "    save();",
    "    [1].forEach(async () => save());",
    "}",
    "",
].join("\n");

const screen = [
    "// eslint-disable",
    "export function Probe({ save }: { save: () => Promise<void> }) {",
    "    return (",
    "        <div onClick={save}>",
    '            <img src="/logo.svg" />',
    "        </div>",
    "    );",
    "}",
    "",
].join("\n");

test("type-aware rules hold in every authored TypeScript folder", async () => {
    for (const file of [
        "src/lib/routes.ts",
        "tests/support/errors.ts",
        "integration/support/console.ts",
        "testing/coverage-options.ts",
        "next.config.ts",
    ]) {
        expect(await rules(file, promises), file).toEqual([
            "@typescript-eslint/no-floating-promises",
            "@typescript-eslint/no-misused-promises",
        ]);
    }
});

test("accessibility rules hold in every component", async () => {
    for (const file of [
        "src/features/auth/steps/auth-fields.tsx",
        "src/ui/ui.tsx",
        "src/app/layout.tsx",
    ]) {
        expect(await rules(file, screen), file).toEqual([
            "@next/next/no-img-element",
            "@typescript-eslint/no-misused-promises",
            "jsx-a11y-x/alt-text",
            "jsx-a11y-x/click-events-have-key-events",
            "jsx-a11y-x/no-static-element-interactions",
        ]);
    }
});

test("code generated from strings is refused in every source kind", async () => {
    const typed = [
        "export function probe(source: string) {",
        "    eval(source);",
        "    setTimeout(source, 1);",
        "    return new Function(source);",
        "}",
        "",
    ].join("\n");
    for (const file of [
        "src/lib/routes.ts",
        "src/ui/ui.tsx",
        "tests/support/errors.ts",
    ]) {
        expect(await rules(file, typed), file).toEqual([
            "@typescript-eslint/no-implied-eval",
            "@typescript-eslint/no-implied-eval",
            "no-eval",
        ]);
    }
    expect(
        await rules(
            "testing/run-coverage.mjs",
            [
                "export function probe(source) {",
                "    eval(source);",
                '    globalThis.setTimeout("probe()", 1);',
                "    return new Function(source);",
                "}",
                "",
            ].join("\n"),
        ),
    ).toEqual(["no-eval", "no-implied-eval", "no-new-func"]);
});

test("the admin console scripts refuse code generated from strings", async () => {
    const { scripts } = JSON.parse(
        readFileSync(path.join(web, "package.json"), "utf8"),
    ) as { scripts: Record<string, string> };
    const [, config, directory] =
        /&& cd \.\. && eslint --config (\S+) (\S+)$/.exec(scripts.lint ?? "") ??
        [];
    expect(directory).toBe("src/pyracast/regions/static/pyracast");
    const root = path.join(web, "..");
    const consoleScripts = new ESLint({
        cwd: root,
        overrideConfigFile: config,
    });
    const probe = [
        '"use strict";',
        "eval(source);",
        'setTimeout("probe()", 1);',
        "new Function(source);",
        "",
    ].join("\n");
    const ruleIds = async (script: string) => {
        const [result] = await consoleScripts.lintText(probe, {
            filePath: path.join(root, directory!, script),
        });
        return result!.messages.map((message) => message.ruleId).sort();
    };
    for (const script of ["admin-historical.js", "admin-workspace.js"])
        expect(await ruleIds(script), script).toEqual([
            "no-eval",
            "no-implied-eval",
            "no-new-func",
        ]);
    expect(await ruleIds("hypothetical.js")).toEqual([]);
});

test("configuration scripts are linted as JavaScript modules", async () => {
    expect(
        await rules(
            "testing/run-coverage.mjs",
            "const unused = 1;\nexport {};\n",
        ),
    ).toEqual(["no-unused-vars"]);
});

const clockRule = async (source: string[]) => {
    const [result] = await eslint.lintText(source.join("\n") + "\n", {
        filePath: path.join(web, "tests/browser-workers.spec.ts"),
    });
    return result!.messages
        .filter((message) => message.ruleId === "pyracast/browser-clock")
        .map((message) => message.line);
};

test("a browser case cannot reach the real date past the shared clock", async () => {
    expect(
        await clockRule([
            'import { test } from "@playwright/test";',
            'test("probe", async ({ page }) => {',
            '    await page.goto("/");',
            "});",
            'test.describe("group", () => {',
            '    test("probe", async ({ context }) => {',
            "        await context.newPage();",
            "    });",
            "});",
            'test("node probe", () => {});',
        ]),
    ).toEqual([2, 6]);
    expect(
        await clockRule([
            'import { test, instant } from "./support/test";',
            'test("probe", async ({ page, browser }) => {',
            "    await page.clock.install();",
            "    await page.clock.install({});",
            "    await page.clock.install({ time: instant });",
            "    await page.clock.setFixedTime(Date.now());",
            "    const today = new Date();",
            "    const fixed = new Date(instant.getTime() + 1);",
            "    await page.evaluate(() => Date.now() - new Date().getTime());",
            "    await browser.newContext();",
            "    await browser.newPage();",
            "    await browser.newContext({ javaScriptEnabled: false });",
            "    void today, fixed, performance.now();",
            "});",
        ]),
    ).toEqual([3, 4, 6, 7, 10, 11]);
});

test("a clock time, script switch or fixture the rule cannot resolve is refused", async () => {
    expect(
        await clockRule([
            'import { test, instant } from "./support/test";',
            "const options = { timezoneId: 'UTC' };",
            'test("probe", async ({ page, browser }) => {',
            "    await page.clock.install({ time: undefined });",
            "    await page.clock.install({ time: void 0 });",
            "    await page.clock.install({ time: null });",
            "    await page.clock.install({ time: instant, ...options });",
            "    await page.clock.install({ ...options, time: instant });",
            "    await page.clock.install({ time: instant as Date });",
            "    await browser.newContext({ javaScriptEnabled: false, ...options });",
            "    await browser.newContext({ ...options, javaScriptEnabled: false });",
            "    await browser.newPage({ javaScriptEnabled: false, javaScriptEnabled: true });",
            "});",
            "test.use({ ...options });",
            "test.use({ clock: [async ({}, run) => run(), { auto: true }] });",
            "test.extend({ clock: async ({}, run) => run() });",
            "test.use(options);",
            'test.use({ timezoneId: "Europe/Istanbul" });',
            "test.describe.configure({ mode: 'serial' });",
        ]),
    ).toEqual([4, 5, 6, 7, 10, 12, 14, 15, 16, 17]);
});

test("only a callback a clocked page runs may read the date", async () => {
    expect(
        await clockRule([
            'import { test } from "./support/test";',
            'test("probe", async ({ page, context }) => {',
            "    const shell = await context.newPage();",
            "    await shell.evaluate(() => Date.now());",
            '    await page.getByRole("table").evaluate(() => new Date());',
            "    await page.mainFrame().evaluate(() => Date.now());",
            "    await context.addInitScript(() => Date.now());",
            '    const worker = await page.waitForEvent("worker");',
            "    await worker.evaluate(() => new Date());",
            "    const helper = { evaluate: (read: () => number) => read() };",
            "    helper.evaluate(() => Date.now());",
            "});",
        ]),
    ).toEqual([9, 11]);
});
