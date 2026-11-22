import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { ESLint } from "eslint";
import { expect, test } from "@playwright/test";
import { brand, brandProperties } from "../src/styles/tokens";

const web = path.resolve();
const eslint = new ESLint({ cwd: web });
async function findings(
    file: string,
    source: string,
    rule = "pyracast/design-tokens",
) {
    const [result] = await eslint.lintText(source, {
        filePath: path.join(web, file),
    });
    return result!.messages
        .filter((message) => message.ruleId === rule)
        .map((message) => message.line);
}

test("a brand property is declared only in the tokens module", async () => {
    const declared = (file: string, source: string) =>
        findings(file, source, "pyracast/brand-source");
    expect(
        await declared(
            "src/styles/tokens.css",
            ":root {\n    --forest : rgb(9 56 58) !important;\n}\n",
        ),
    ).toEqual([2]);
    expect(
        await declared(
            "src/app/globals.css",
            ".site {\n    --forest: var(--teal);\n}\n",
        ),
    ).toEqual([2]);
    expect(
        await declared(
            "src/features/public-site/public-site.module.css",
            ".probe{--amber-61:#fff;--cream/**/:var(--line)}\n",
        ),
    ).toEqual([1, 1]);
    expect(
        await declared(
            "src/app/globals.css",
            ".probe {\n    --forestry: var(--line);\n    --ink: var(--forest);\n    color: var(--amber);\n}\n",
        ),
    ).toEqual([]);
    expect(
        await declared(
            "src/features/auth/steps/auth-story.tsx",
            'export const style = { "--cream": "var(--line)" };\n',
        ),
    ).toEqual([1]);
    expect(
        await declared(
            "src/styles/tokens.ts",
            'export const style = { "--cream": "var(--line)" };\n',
        ),
    ).toEqual([]);
});

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
            "src/features/auth/steps/auth-story.tsx",
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
            "src/features/auth/steps/auth-story.tsx",
            '// eslint-disable-next-line\nexport const colour = "#a17537";\n',
        ),
    ).toEqual([2]);
});

test("scripts take colours from the tokens module", async () => {
    expect(
        await findings(
            "src/features/auth/steps/auth-story.tsx",
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

const kitArtwork = "src/app/icon.svg";

function hsl(hex: string) {
    const [r, g, b] = [1, 3, 5].map(
        (start) => Number.parseInt(hex.slice(start, start + 2), 16) / 255,
    ) as [number, number, number];
    const high = Math.max(r, g, b);
    const low = Math.min(r, g, b);
    const chroma = high - low;
    const sector =
        high === r
            ? ((g - b) / chroma + 6) % 6
            : high === g
              ? (b - r) / chroma + 2
              : (r - g) / chroma + 4;
    return { hue: chroma === 0 ? 0 : sector * 60, lightness: (high + low) / 2 };
}

async function sourceFiles() {
    const entries = await readdir(path.join(web, "src"), {
        recursive: true,
        withFileTypes: true,
    });
    return entries
        .filter((entry) => entry.isFile())
        .map((entry) =>
            path
                .relative(web, path.join(entry.parentPath, entry.name))
                .replaceAll(path.sep, "/"),
        );
}

test("the brand families are written once, in the tokens module", async () => {
    const stylesheet = await readFile(
        path.join(web, "src/styles/tokens.css"),
        "utf8",
    );
    expect(stylesheet.match(/--(?:forest|amber|cream)[\w-]*(?=:)/g)).toBeNull();
    const values = Object.values(brandProperties).map((value) =>
        value.slice(0, 7),
    );
    const written: string[] = [];
    for (const file of await sourceFiles()) {
        if (file === "src/styles/tokens.ts" || file === kitArtwork) continue;
        const text = (
            await readFile(path.join(web, file), "utf8")
        ).toLowerCase();
        for (const value of values)
            if (text.includes(value)) written.push(`${file} ${value}`);
    }
    expect(written).toEqual([]);
});

test("every brand shade takes its kit colour's hue and is named by its lightness", () => {
    for (const [name, value] of Object.entries(brandProperties)) {
        const [, family, number, alpha] =
            /^--(forest|amber|cream)(?:-(\d+))?(?:-a(\d+))?$/.exec(name)!;
        const kit = brand[family as keyof typeof brand];
        if (number === undefined) {
            expect(value, name).toBe(kit);
            continue;
        }
        const shade = hsl(value);
        expect(Math.abs(shade.hue - hsl(kit).hue), name).toBeLessThan(2);
        expect(Math.round(1000 * (1 - shade.lightness)), name).toBe(
            Number(number),
        );
        if (alpha === undefined) expect(value, name).toHaveLength(7);
        else
            expect(
                Math.round((Number.parseInt(value.slice(7), 16) / 255) * 100),
                name,
            ).toBe(Number(alpha));
    }
});
