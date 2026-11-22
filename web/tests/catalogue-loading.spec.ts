import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { loadCatalogue } from "../src/i18n/catalogue";
import { messages } from "../src/i18n/messages";
import enCore from "../messages/en/core.json";
import enForecast from "../messages/en/forecast.json";
import enShell from "../messages/en/shell.json";
import enWorkspace from "../messages/en/workspace.json";
import trCore from "../messages/tr/core.json";
import trForecast from "../messages/tr/forecast.json";
import trShell from "../messages/tr/shell.json";
import trWorkspace from "../messages/tr/workspace.json";

const web = path.resolve();
const full = [
    "src/i18n/messages.ts",
    "src/i18n/en.ts",
    "src/i18n/tr.ts",
    ...["en", "tr"].flatMap((language) =>
        ["core", "forecast", "workspace"].map(
            (part) => `messages/${language}/${part}.json`,
        ),
    ),
];

async function sources(folder: string): Promise<string[]> {
    const entries = await readdir(folder, { withFileTypes: true });
    const files: string[] = [];
    for (const entry of entries) {
        const file = path.join(folder, entry.name);
        if (entry.isDirectory()) files.push(...(await sources(file)));
        else if (/\.tsx?$/.test(entry.name)) files.push(file);
    }
    return files;
}

function resolved(file: string, specifier: string) {
    const target = specifier.startsWith("@/")
        ? path.join(web, "src", specifier.slice(2))
        : path.resolve(path.dirname(file), specifier);
    const name = path.relative(web, target).replaceAll(path.sep, "/");
    return name.endsWith(".json") ? name : `${name}.ts`;
}

const moduleReferences = [
    /^(?:import|export) (?!type )[^;]*? from "([^"]+)";$/gms,
    /^import "([^"]+)";$/gm,
    /\bimport\(\s*["'`]([^"'`]+)["'`]\s*\)/g,
];

function referenced(source: string) {
    const text = source.replace(/^\uFEFF/, "");
    return moduleReferences.flatMap((pattern) =>
        [...text.matchAll(pattern)].map(([, specifier]) => specifier!),
    );
}

test("the module scan sees static imports, re-exports and dynamic imports", () => {
    expect(
        referenced(
            [
                'import { a } from "./static";',
                'import type { b } from "./types-only";',
                'import "./side-effect";',
                'export { messages } from "@/i18n/messages";',
                'export * from "./everything";',
                'export type { c } from "./types-only";',
                'const d = () => import("./later");',
            ].join("\n"),
        ).sort(),
    ).toEqual(
        [
            "./static",
            "./side-effect",
            "@/i18n/messages",
            "./everything",
            "./later",
        ].sort(),
    );
});

test("only the server configuration and the active-language loader reach the full catalogues", async () => {
    const importers: Record<string, string[]> = {};
    for (const file of await sources(path.join(web, "src"))) {
        const source = await readFile(file, "utf8");
        const reached = referenced(source)
            .map((specifier) => resolved(file, specifier))
            .filter((target) => full.includes(target));
        if (reached.length)
            importers[path.relative(web, file).replaceAll(path.sep, "/")] =
                reached.sort();
    }
    expect(importers).toEqual({
        "src/i18n/catalogue.ts": ["src/i18n/en.ts", "src/i18n/tr.ts"],
        "src/i18n/en.ts": [
            "messages/en/core.json",
            "messages/en/forecast.json",
            "messages/en/workspace.json",
        ],
        "src/i18n/messages.ts": ["src/i18n/en.ts", "src/i18n/tr.ts"],
        "src/i18n/request.ts": ["src/i18n/messages.ts"],
        "src/i18n/tr.ts": [
            "messages/tr/core.json",
            "messages/tr/forecast.json",
            "messages/tr/workspace.json",
        ],
    });
});

test("each full catalogue holds every namespace of its parts exactly once", () => {
    for (const [language, parts] of [
        ["en", [enShell, enCore, enForecast, enWorkspace]],
        ["tr", [trShell, trCore, trForecast, trWorkspace]],
    ] as const) {
        const namespaces = parts.flatMap((part) => Object.keys(part));
        expect(new Set(namespaces).size, language).toBe(namespaces.length);
        expect(Object.keys(messages[language]).sort(), language).toEqual(
            namespaces.sort(),
        );
    }
});

test("the shell catalogue holds only the texts shown before or outside a full catalogue", () => {
    for (const shell of [enShell, trShell])
        expect(Object.keys(shell).sort()).toEqual([
            "AppError",
            "Bootstrap",
            "Errors",
            "Language",
        ]);
});

test("a catalogue loads once and resolves to the full messages of its language", async () => {
    const first = loadCatalogue("tr");
    expect(loadCatalogue("tr")).toBe(first);
    expect(await first).toEqual(messages.tr);
    expect(await loadCatalogue("tr")).toBe(await first);
    expect(await loadCatalogue("en")).toEqual(messages.en);
});
