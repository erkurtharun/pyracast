import { readFileSync } from "node:fs";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { ESLint } from "eslint";
import { expect, test } from "@playwright/test";
import { brandProperties, imageShades } from "../src/styles/tokens";
import artwork from "./support/brand-artwork.json";

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
            "public/identity/probe.css",
            ".probe {\n    --forest: var(--line);\n}\n",
        ),
    ).toEqual([2]);
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

function channels(hex: string) {
    return [1, 3, 5].map(
        (start) => Number.parseInt(hex.slice(start, start + 2), 16) / 255,
    ) as [number, number, number];
}

function hsl(hex: string) {
    const [r, g, b] = channels(hex);
    const high = Math.max(r, g, b);
    const low = Math.min(r, g, b);
    const chroma = high - low;
    const lightness = (high + low) / 2;
    const sector =
        high === r
            ? ((g - b) / chroma + 6) % 6
            : high === g
              ? (b - r) / chroma + 2
              : (r - g) / chroma + 4;
    return {
        hue: chroma === 0 ? 0 : sector * 60,
        saturation:
            chroma === 0 ? 0 : chroma / (1 - Math.abs(2 * lightness - 1)),
        lightness,
    };
}

function hex(hue: number, saturation: number, lightness: number) {
    const chroma = (1 - Math.abs(2 * lightness - 1)) * saturation;
    const second = chroma * (1 - Math.abs(((hue / 60) % 2) - 1));
    const sector = Math.floor(hue / 60);
    const [r, g, b] = [
        [chroma, second, 0],
        [second, chroma, 0],
        [0, chroma, second],
        [0, second, chroma],
        [second, 0, chroma],
        [chroma, 0, second],
    ][sector]!;
    const lowest = lightness - chroma / 2;
    return `#${[r, g, b]
        .map((value) =>
            Math.round((value! + lowest) * 255)
                .toString(16)
                .padStart(2, "0"),
        )
        .join("")}`;
}

function contrast(first: string, second: string) {
    const [high, low] = [first, second]
        .map((colour) => {
            const [r, g, b] = channels(colour).map((value) =>
                value <= 0.04045
                    ? value / 12.92
                    : ((value + 0.055) / 1.055) ** 2.4,
            ) as [number, number, number];
            return 0.2126 * r + 0.7152 * g + 0.0722 * b;
        })
        .sort((a, b) => b - a) as [number, number];
    return (high + 0.05) / (low + 0.05);
}

const families = {
    forest: { kit: artwork.colours.dark, old: "#143c32" },
    amber: { kit: artwork.colours.sun, old: "#a56a12" },
    cream: { kit: artwork.colours.light, old: "#f6f7f3" },
    teal: { kit: artwork.colours.dark, old: "#176b56" },
};

function composite(colour: string, alpha: number, surface: string) {
    const [top, bottom] = [colour, surface].map(channels);
    return `#${top!
        .map((value, index) =>
            Math.round((value * alpha + bottom![index]! * (1 - alpha)) * 255)
                .toString(16)
                .padStart(2, "0"),
        )
        .join("")}`;
}

type Token = `--${string}`;
type Shade = Token | { colour: Token; alpha: number; over: Token };
type Pairing = { pair: [Shade, Shade] } & (
    { before: [string, string] } | { atLeast: number }
);

const shades: Record<string, string> = { ...brandProperties, ...imageShades };

const tokens: Record<string, string> = {
    ...Object.fromEntries(
        [
            ...readFileSync(
                path.join(web, "src/styles/tokens.css"),
                "utf8",
            ).matchAll(/^\s*(--[\w-]+):\s*#([0-9a-f]{3}|[0-9a-f]{6});$/gim),
        ].map(([, name, digits]) => [
            name!,
            `#${digits!.length === 3 ? [...digits!].map((digit) => digit + digit).join("") : digits}`.toLowerCase(),
        ]),
    ),
    ...shades,
};

// The densest forecast fill the production line crosses: 15.3 % of the series over the panel.
const forecastFillUnderProduction: Shade = {
    colour: "--green-722",
    alpha: 0.153,
    over: "--olive-80",
};

const derivation: Record<string, { from: string; keeps?: Pairing }> = {
    "--forest-822": {
        from: "#20483c",
        keeps: {
            pair: ["--cream", "--forest-822"],
            before: ["#f4f7ee", "#20483c"],
        },
    },
    "--amber-65": { from: "#fbf3e2" },
    "--amber-69": { from: "#f7f1e4" },
    "--amber-220": { from: "#e6d2a8" },
    "--amber-296": { from: "#efc778" },
    "--amber-300": { from: "#d2c293" },
    "--amber-329-a0": { from: "#e8bd6e00" },
    "--amber-329-a20": { from: "#e8bd6e33" },
    "--amber-608": {
        from: "#a17537",
        keeps: {
            pair: ["--amber-608", forecastFillUnderProduction],
            atLeast: 3,
        },
    },
    "--amber-684": {
        from: "#a56a12",
        keeps: {
            pair: ["--amber-684", "--olive-100"],
            before: ["#946827", "#e8ecdf"],
        },
    },
    "--amber-755": {
        from: "#7a4d08",
        keeps: {
            pair: ["--amber-755", "--amber-65"],
            before: ["#7a4d08", "#fbf3e2"],
        },
    },
    "--cream-39": { from: "#f6f7f3" },
    "--cream-45": {
        from: "#f6f7f3",
        keeps: {
            pair: ["--white", "--cream-45"],
            before: ["#ffffff", "#f6f7f3"],
        },
    },
    "--teal-65": { from: "#eaf3ed" },
    "--teal-708": {
        from: "#26735f",
        keeps: {
            pair: ["--white", "--teal-708"],
            before: ["#ffffff", "#26735f"],
        },
    },
    "--teal-729": {
        from: "#28664e",
        keeps: {
            pair: ["--white", "--teal-729"],
            before: ["#ffffff", "#28664e"],
        },
    },
    "--teal-753": {
        from: "#176b56",
        keeps: {
            pair: ["--white", "--teal-753"],
            before: ["#ffffff", "#176b56"],
        },
    },
    "--teal-802": {
        from: "#1c4d3a",
        keeps: {
            pair: ["--white", "--teal-802"],
            before: ["#ffffff", "#1c4d3a"],
        },
    },
    "--teal-824": {
        from: "#104d3f",
        keeps: {
            pair: ["--white", "--teal-824"],
            before: ["#ffffff", "#104d3f"],
        },
    },
    "--teal-841-a3": { from: "#173a3608" },
    "--teal-841-a4": { from: "#173a360a" },
    "--teal-841-a5": { from: "#173a360d" },
};

function ruleShade(name: string, lightness: number) {
    const [, family] = /^--(\w+)-/.exec(name)!;
    const { kit, old } = families[family as keyof typeof families];
    const { from } = derivation[name]!;
    const saturation = Math.min(
        1,
        (hsl(from).saturation * hsl(kit).saturation) / hsl(old).saturation,
    );
    return hex(hsl(kit).hue, saturation, lightness) + from.slice(7);
}

function measured(pairing: Pairing, name: string, value: string) {
    const resolve = (token: Token) => {
        if (token === name) return value;
        expect(tokens[token], token).toBeDefined();
        return tokens[token]!;
    };
    const [first, second] = pairing.pair.map((shade) =>
        typeof shade === "string"
            ? resolve(shade)
            : composite(
                  resolve(shade.colour),
                  shade.alpha,
                  resolve(shade.over),
              ),
    ) as [string, string];
    return contrast(first, second);
}

test("every brand shade is the kit's colour or follows the token rule, and each exception keeps its pairing", () => {
    expect(Object.keys(derivation).sort()).toEqual(
        Object.keys(shades)
            .filter((name) => /-\d/.test(name))
            .sort(),
    );
    for (const [name, value] of Object.entries(shades)) {
        const [, family, number, alpha] =
            /^--(forest|amber|cream|teal)(?:-(\d+))?(?:-a(\d+))?$/.exec(name)!;
        if (alpha !== undefined)
            expect(
                Math.round((Number.parseInt(value.slice(7), 16) / 255) * 100),
                name,
            ).toBe(Number(alpha));
        if (number === undefined) {
            expect(value, name).toBe(
                families[family as keyof typeof families].kit +
                    (alpha === undefined ? "" : value.slice(7)),
            );
            continue;
        }
        expect(value, name).toBe(ruleShade(name, 1 - Number(number) / 1000));
        const { from, keeps } = derivation[name]!;
        const oldNumber = Math.round(1000 * (1 - hsl(from).lightness));
        if (keeps === undefined) {
            expect(Number(number), name).toBe(oldNumber);
            continue;
        }
        const floor =
            "atLeast" in keeps ? keeps.atLeast : contrast(...keeps.before);
        expect(measured(keeps, name, value), name).toBeGreaterThanOrEqual(
            floor,
        );
        expect(
            measured(keeps, name, ruleShade(name, 1 - oldNumber / 1000)),
            name,
        ).toBeLessThan(floor);
    }
});

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

test("the brand shades are written once, in the tokens module", async () => {
    const values = Object.values(shades).map((value) => value.slice(0, 7));
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

test("the root document carries exactly the brand shades a stylesheet reads", async () => {
    const read = new Set<string>();
    for (const file of await sourceFiles())
        if (file.endsWith(".css"))
            for (const [, name] of (
                await readFile(path.join(web, file), "utf8")
            ).matchAll(/var\(\s*(--[\w-]+)/g))
                read.add(name!);
    expect(
        Object.keys(brandProperties).filter((name) => !read.has(name)),
    ).toEqual([]);
    expect(Object.keys(imageShades).filter((name) => read.has(name))).toEqual(
        [],
    );
});

test("the console's brand tokens are the shades of the tokens module", async () => {
    const stylesheet = await readFile(
        path.join(
            web,
            "../src/pyracast/regions/static/pyracast/admin-workspace.css",
        ),
        "utf8",
    );
    const [, root] = /^:root \{\n([^}]*)\n\}/.exec(stylesheet)!;
    const declared = [
        ...root!.matchAll(
            /^\s*(--(?:forest|amber|cream|teal)[\w-]*): (#\w+);$/gm,
        ),
    ];
    expect(declared.length).toBeGreaterThan(3);
    for (const [, name, value] of declared) {
        const [, family, number, alpha] =
            /^--(forest|amber|cream|teal)(?:-(\d+))?(?:-a(\d+))?$/.exec(name!)!;
        if (number !== undefined) {
            expect(value, name).toBe(shades[name!]);
            continue;
        }
        expect(value, name).toBe(
            families[family as keyof typeof families].kit +
                (alpha === undefined ? "" : value!.slice(7)),
        );
        if (alpha !== undefined)
            expect(
                Math.round((Number.parseInt(value!.slice(7), 16) / 255) * 100),
                name,
            ).toBe(Number(alpha));
    }
});
