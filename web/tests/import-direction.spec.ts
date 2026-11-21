import path from "node:path";
import { ESLint } from "eslint";
import { expect, test } from "@playwright/test";

const web = path.resolve();
const eslint = new ESLint({ cwd: web });

async function restricted(file: string, source: string) {
    const [result] = await eslint.lintText(`${source}\nexport {};\n`, {
        filePath: path.join(web, file),
    });
    return result!.messages
        .filter((message) => message.ruleId === "no-restricted-imports")
        .map((message) => message.line);
}

const imports = (specifier: string) => `import "${specifier}";`;

test("lib and i18n import neither screens nor routes", async () => {
    for (const file of ["src/lib/routes.ts", "src/i18n/language.ts"]) {
        for (const specifier of [
            "@/features/session/app-root",
            "@/ui/ui",
            "@/app/app/page",
            "@/components/ui",
            "../features/session/app-root",
            "../ui/ui",
            "../app/app/page",
        ])
            expect(
                await restricted(file, imports(specifier)),
                `${file} ${specifier}`,
            ).toEqual([1]);
        expect(await restricted(file, imports("@/lib/routes"))).toEqual([]);
    }
    expect(
        await restricted("src/lib/api/keys.ts", imports("../../ui/ui")),
    ).toEqual([1]);
    expect(
        await restricted("src/lib/api/keys.ts", imports("../schema")),
    ).toEqual([]);
});

test("ui imports no feature and no route", async () => {
    for (const specifier of [
        "@/features/auth/auth",
        "@/app/app/page",
        "../features/auth/auth",
    ])
        expect(
            await restricted("src/ui/ui.tsx", imports(specifier)),
            specifier,
        ).toEqual([1]);
    for (const specifier of ["@/lib/routes", "./ui", "@/i18n/language"])
        expect(
            await restricted("src/ui/ui.tsx", imports(specifier)),
            specifier,
        ).toEqual([]);
});

test("a feature reaches another feature only through its top-level modules", async () => {
    const feature = "src/features/auth/auth.tsx";
    for (const specifier of [
        "@/features/account/two-factor/machine",
        "../account/two-factor-settings",
        "@/app/app/page",
    ])
        expect(
            await restricted(feature, imports(specifier)),
            specifier,
        ).toEqual([1]);
    for (const specifier of [
        "@/features/account/two-factor-settings",
        "./steps/sign-in",
        "@/ui/ui",
        "@/lib/routes",
    ])
        expect(
            await restricted(feature, imports(specifier)),
            specifier,
        ).toEqual([]);
    expect(
        await restricted(
            "src/features/account/two-factor/model.ts",
            imports("../two-factor-settings"),
        ),
    ).toEqual([]);
    expect(
        await restricted(
            "src/features/account/two-factor/model.ts",
            imports("../../auth/auth"),
        ),
    ).toEqual([1]);
});

test("routes use features only through their top-level modules", async () => {
    const route = "src/app/app/w/[tenantId]/page.tsx";
    expect(
        await restricted(route, imports("@/features/auth/steps/sign-in")),
    ).toEqual([1]);
    expect(await restricted(route, imports("@/components/portfolio"))).toEqual([
        1,
    ]);
    expect(
        await restricted(
            "src/app/app/page.tsx",
            imports("../../features/auth/steps/submit"),
        ),
    ).toEqual([1]);
    expect(
        await restricted(
            "src/app/app/page.tsx",
            imports("../../features/portfolio/portfolio"),
        ),
    ).toEqual([]);
    expect(
        await restricted("src/app/app/not-found.tsx", imports("../not-found")),
    ).toEqual([]);
    expect(
        await restricted(route, imports("@/features/portfolio/portfolio")),
    ).toEqual([]);
});
