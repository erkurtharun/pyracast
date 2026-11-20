import path from "node:path";
import { ESLint } from "eslint";
import { expect, test } from "@playwright/test";

const web = path.resolve();
const eslint = new ESLint({ cwd: web });

async function rules(file: string, source: string) {
    const [result] = await eslint.lintText(source, {
        filePath: path.join(web, file),
    });
    return result!.messages.map((message) => message.ruleId).sort();
}

const screen = [
    "// eslint-disable",
    "export function Probe({ save }: { save: () => Promise<void> }) {",
    "    save();",
    "    return (",
    "        <div onClick={save}>",
    '            <img src="/logo.svg" />',
    "        </div>",
    "    );",
    "}",
    "",
].join("\n");

test("type-aware and accessibility rules hold in every authored TypeScript folder", async () => {
    for (const file of [
        "src/features/auth/steps/auth-fields.tsx",
        "src/ui/ui.tsx",
        "tests/support/test.tsx",
        "integration/support/console.tsx",
        "testing/coverage-options.tsx",
    ]) {
        expect(await rules(file, screen), file).toEqual([
            "@next/next/no-img-element",
            "@typescript-eslint/no-floating-promises",
            "@typescript-eslint/no-misused-promises",
            "jsx-a11y-x/alt-text",
            "jsx-a11y-x/click-events-have-key-events",
            "jsx-a11y-x/no-static-element-interactions",
        ]);
    }
});

test("configuration scripts are linted as JavaScript modules", async () => {
    expect(
        await rules("testing/probe.mjs", "const unused = 1;\nexport {};\n"),
    ).toEqual(["no-unused-vars"]);
});
