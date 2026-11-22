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
        "tests/support/test.ts",
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
        "tests/support/test.ts",
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
    for (const script of ["admin-historical.js", "admin-workspace.js"]) {
        const [result] = await consoleScripts.lintText(probe, {
            filePath: path.join(root, directory!, script),
        });
        expect(
            result!.messages.map((message) => message.ruleId).sort(),
            script,
        ).toEqual(["no-eval", "no-implied-eval", "no-new-func"]);
    }
});

test("configuration scripts are linted as JavaScript modules", async () => {
    expect(
        await rules(
            "testing/run-coverage.mjs",
            "const unused = 1;\nexport {};\n",
        ),
    ).toEqual(["no-unused-vars"]);
});
