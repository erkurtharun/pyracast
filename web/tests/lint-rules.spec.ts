import path from "node:path";
import { ESLint } from "eslint";
import { expect, test } from "@playwright/test";

const web = path.resolve();
const eslint = new ESLint({ cwd: web });

// Type-aware rules parse through the TypeScript project, so each probe lints an existing file.
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

test("configuration scripts are linted as JavaScript modules", async () => {
    expect(
        await rules(
            "testing/run-coverage.mjs",
            "const unused = 1;\nexport {};\n",
        ),
    ).toEqual(["no-unused-vars"]);
});
