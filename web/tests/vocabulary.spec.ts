import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { vocabulary } from "../src/lib/vocabulary";
import enCore from "../messages/en/core.json";
import trCore from "../messages/tr/core.json";

const web = path.resolve();
type Schemas = Record<string, { enum?: unknown[] }>;

async function schemas(): Promise<Schemas> {
    const document = JSON.parse(
        await readFile(path.join(web, "openapi.json"), "utf8"),
    );
    return document.components.schemas;
}

async function sources(directory: string): Promise<string[]> {
    const entries = await readdir(directory, {
        recursive: true,
        withFileTypes: true,
    });
    return entries
        .filter((entry) => entry.isFile() && /\.tsx?$/.test(entry.name))
        .map((entry) => path.join(entry.parentPath, entry.name));
}

test("every label group names a generated enumeration and labels each of its values in both languages", async () => {
    const generated = await schemas();
    const catalogs = { en: enCore.Enums, tr: trCore.Enums } as Record<
        string,
        Record<string, Record<string, string>>
    >;
    for (const [group, schema] of Object.entries(vocabulary)) {
        const values = generated[schema]?.enum;
        expect(values, `${schema} is a generated enumeration`).toBeDefined();
        for (const [language, catalog] of Object.entries(catalogs))
            expect(
                Object.keys(catalog[group] ?? {}).sort(),
                `${language} Enums.${group}`,
            ).toEqual(values!.map(String).sort());
    }
    for (const catalog of Object.values(catalogs))
        expect(Object.keys(catalog).sort()).toEqual(
            Object.keys(vocabulary).sort(),
        );
});

function keys(node: unknown, prefix = ""): string[] {
    return node && typeof node === "object"
        ? Object.entries(node).flatMap(([key, value]) =>
              keys(value, `${prefix}${key}.`),
          )
        : [prefix.slice(0, -1)];
}

test("both languages carry the same message keys", async () => {
    for (const name of ["core", "workspace", "forecast"]) {
        const [en, tr] = await Promise.all(
            ["en", "tr"].map(async (language) =>
                JSON.parse(
                    await readFile(
                        path.join(web, "messages", language, `${name}.json`),
                        "utf8",
                    ),
                ),
            ),
        );
        expect(keys(tr).sort(), name).toEqual(keys(en).sort());
    }
});

test("screens label statuses and kinds only through the shared vocabulary", async () => {
    const files = [
        ...(await sources(path.join(web, "src/features"))),
        ...(await sources(path.join(web, "src/ui"))),
    ].filter((file) => !file.endsWith(path.join("ui", "vocabulary.tsx")));
    const adHoc: string[] = [];
    for (const file of files) {
        const source = await readFile(file, "utf8");
        source.split("\n").forEach((line, index) => {
            if (
                /Record<string, string> = \{/.test(line) ||
                /\bt\.has\(|\.has\(`/.test(line) ||
                /statuses\./.test(line)
            )
                adHoc.push(`${path.relative(web, file)}:${index + 1}`);
        });
    }
    expect(adHoc).toEqual([]);
});
