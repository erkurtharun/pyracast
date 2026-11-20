import path from "node:path";
import { ESLint } from "eslint";
import { expect, test } from "@playwright/test";

const web = path.resolve();
const eslint = new ESLint({ cwd: web });
const rule = "pyracast/design-tokens";

async function findings(file: string, source: string) {
    const [result] = await eslint.lintText(source, {
        filePath: path.join(web, file),
    });
    return result!.messages
        .filter((message) => message.ruleId === rule)
        .map((message) => message.line);
}

test("stylesheets take colours and spacing only from the tokens", async () => {
    for (const file of [
        "src/app/globals.css",
        "src/features/public-site/public-site.module.css",
        "src/features/probe/probe.module.css",
    ]) {
        expect(
            await findings(
                file,
                ".probe {\n    color: #a17537;\n    padding: 12px 0;\n    gap: var(--space-2);\n    margin: 0 auto;\n    border: 1px solid var(--line);\n}\n",
            ),
            file,
        ).toEqual([2, 3]);
    }
    expect(
        await findings(
            "src/styles/tokens.css",
            ":root {\n    --brand: #a17537;\n    --space-2: 8px;\n}\n",
        ),
    ).toEqual([]);
});

test("colour functions, named colours and viewport or font lengths are literals too", async () => {
    expect(
        await findings(
            "src/app/globals.css",
            [
                ".probe {",
                "    background: rgb(0 0 0 / 10%);",
                "    color: Red;",
                "    border-color: oklch(70% 0.1 150);",
                "    padding: 2vw 1ch;",
                "    margin: 5% auto;",
                "    background: color-mix(in srgb, var(--line) 50%, transparent);",
                "    border: 1px solid currentColor;",
                "}",
                "",
            ].join("\n"),
        ),
    ).toEqual([2, 3, 4, 5]);
});

test("inline styles take spacing from the tokens", async () => {
    expect(
        await findings(
            "src/features/probe/probe.tsx",
            [
                "export const box = (",
                "    <div",
                "        style={{",
                "            padding: 20,",
                '            gap: "1rem",',
                "            margin: 0,",
                '            shadow: "rgba(0, 0, 0, 0.2)",',
                '            height: "50%",',
                "        }}",
                "    />",
                ");",
                "",
            ].join("\n"),
        ),
    ).toEqual([4, 5, 7]);
});

test("an inline directive cannot switch the token rule off", async () => {
    expect(
        await findings(
            "src/app/globals.css",
            "/* eslint-disable */\n.probe {\n    color: #fff;\n}\n",
        ),
    ).toEqual([3]);
    expect(
        await findings(
            "src/features/probe/probe.tsx",
            '// eslint-disable-next-line\nexport const colour = "#a17537";\n',
        ),
    ).toEqual([2]);
});

test("scripts take colours from the tokens module", async () => {
    expect(
        await findings(
            "src/features/probe/probe.tsx",
            'export const colour = "#a17537";\nexport const fill = `#fff`;\nexport const anchor = "#members-title";\n',
        ),
    ).toEqual([1, 2]);
    expect(
        await findings(
            "src/styles/tokens.ts",
            'export const brand = "#a17537";\n',
        ),
    ).toEqual([]);
});
