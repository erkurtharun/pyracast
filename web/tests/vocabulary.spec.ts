import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { expect, test } from "@playwright/test";
import ts from "typescript";
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

async function screenFiles() {
    return [
        ...(await sources(path.join(web, "src/features"))),
        ...(await sources(path.join(web, "src/ui"))),
    ].filter((file) => !file.endsWith(path.join("ui", "vocabulary.tsx")));
}

test("screens label statuses and kinds only through the shared vocabulary", async () => {
    const adHoc: string[] = [];
    for (const file of await screenFiles()) {
        const source = await readFile(file, "utf8");
        for (const pattern of [
            /Record<\s*(?:string|[\w.]+\[\s*["']\w+["']\s*\])\s*,\s*string\s*>\s*=\s*\{/g,
            /\bt\.has\(|\.has\(`/g,
            /statuses\./g,
        ])
            for (const match of source.matchAll(pattern))
                adHoc.push(
                    `${path.relative(web, file)}:${source.slice(0, match.index).split("\n").length}`,
                );
    }
    expect(adHoc).toEqual([]);
});

test("screens never show a generated enumeration value or map it to labels themselves", async () => {
    const generated = await schemas();
    const groups = Object.values(vocabulary).map(
        (schema) => new Set(generated[schema]!.enum!.map(String)),
    );
    const inOneGroup = (values: string[]) =>
        values.length > 1 &&
        groups.some((group) => values.every((value) => group.has(value)));
    const files = await screenFiles();
    const program = ts.createProgram(
        files.filter((file) => file.endsWith(".tsx")),
        {
            strict: true,
            jsx: ts.JsxEmit.ReactJSX,
            module: ts.ModuleKind.ESNext,
            moduleResolution: ts.ModuleResolutionKind.Bundler,
            resolveJsonModule: true,
            skipLibCheck: true,
            noEmit: true,
            paths: { "@/*": ["./src/*"] },
            baseUrl: web,
        },
    );
    const checker = program.getTypeChecker();
    const enumValues = (node: ts.Node) => {
        const type = checker.getTypeAtLocation(node);
        const members = (type.isUnion() ? type.types : [type]).filter(
            (member) =>
                !(member.flags & (ts.TypeFlags.Null | ts.TypeFlags.Undefined)),
        );
        return members.every((member) => member.isStringLiteral())
            ? members.map((member) => member.value)
            : [];
    };
    const labelCall = (node: ts.Node | undefined) =>
        !!node &&
        ts.isCallExpression(node) &&
        !!node.arguments[0] &&
        ts.isStringLiteralLike(node.arguments[0]);
    const found: string[] = [];
    for (const file of program.getSourceFiles()) {
        if (!files.includes(path.normalize(file.fileName))) continue;
        const report = (node: ts.Node, kind: string) =>
            found.push(
                `${path.relative(web, file.fileName)}:${file.getLineAndCharacterOfPosition(node.getStart()).line + 1} ${kind}`,
            );
        const visit = (node: ts.Node): void => {
            if (
                ts.isJsxExpression(node) &&
                node.expression &&
                !ts.isJsxAttribute(node.parent) &&
                inOneGroup(enumValues(node.expression))
            )
                report(node, "raw value");
            if (ts.isObjectLiteralExpression(node)) {
                const names = node.properties.flatMap((property) =>
                    ts.isPropertyAssignment(property) &&
                    labelCall(property.initializer)
                        ? [property.name.getText(file).replace(/["']/g, "")]
                        : [],
                );
                if (
                    names.length === node.properties.length &&
                    inOneGroup(names)
                )
                    report(node, "label map");
            }
            if (
                ts.isSwitchStatement(node) &&
                inOneGroup(enumValues(node.expression)) &&
                node.caseBlock.clauses.some((clause) =>
                    clause.statements.some(
                        (statement) =>
                            ts.isReturnStatement(statement) &&
                            labelCall(statement.expression),
                    ),
                )
            )
                report(node, "label switch");
            ts.forEachChild(node, visit);
        };
        visit(file);
    }
    expect(found).toEqual([]);
});
