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

test("only the server configuration reaches both full catalogues", async () => {
    const importers: Record<string, string[]> = {};
    for (const file of await sources(path.join(web, "src"))) {
        const source = await readFile(file, "utf8");
        const reached = [
            ...source.matchAll(/^import (?!type )[^;]*? from "([^"]+)";$/gms),
        ]
            .map(([, specifier]) => resolved(file, specifier!))
            .filter((target) => full.includes(target));
        if (reached.length)
            importers[path.relative(web, file).replaceAll(path.sep, "/")] =
                reached.sort();
    }
    expect(importers).toEqual({
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

test("a catalogue loads once and resolves to the full messages of its language", async () => {
    const first = loadCatalogue("tr");
    expect(loadCatalogue("tr")).toBe(first);
    expect(await first).toEqual(messages.tr);
    expect(await loadCatalogue("tr")).toBe(await first);
    expect(await loadCatalogue("en")).toEqual(messages.en);
});
