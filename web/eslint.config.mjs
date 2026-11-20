import { defineConfig, globalIgnores } from "eslint/config";
import js from "@eslint/js";
import ts from "typescript-eslint";
import next from "@next/eslint-plugin-next";
import hooks from "eslint-plugin-react-hooks";

// Imports point down: app -> features -> ui -> lib and i18n. A feature reaches another one only
// through its top-level modules; nested folders are its internals.
const restrict = (files, ...group) => ({
    files,
    rules: {
        "no-restricted-imports": [
            "error",
            {
                patterns: [
                    {
                        group,
                        message:
                            "Imports point down: app, features, ui, lib; another feature only through its top-level modules.",
                    },
                ],
            },
        ],
    },
});
const screens = ["@/app/**", "@/components/**"];
export default defineConfig([
    globalIgnores([
        ".next/**",
        ".next-*/**",
        "src/lib/schema.d.ts",
        "playwright-report/**",
        "test-results/**",
        "integration-results/**",
        "coverage/**",
        ".dev/**",
        "next-env.d.ts",
    ]),
    {
        files: ["**/*.{ts,tsx}"],
        extends: [
            js.configs.recommended,
            ...ts.configs.recommended,
            hooks.configs.flat.recommended,
        ],
        plugins: { "@next/next": next },
        rules: {
            ...next.configs.recommended.rules,
            ...next.configs["core-web-vitals"].rules,
        },
    },
    restrict(
        ["src/lib/**/*.{ts,tsx}", "src/i18n/**/*.{ts,tsx}"],
        ...screens,
        "@/features/**",
        "@/ui/**",
    ),
    restrict(["src/ui/**/*.{ts,tsx}"], ...screens, "@/features/**", "../*"),
    restrict(
        ["src/features/*/*.{ts,tsx}"],
        ...screens,
        "@/features/*/*/**",
        "../*",
    ),
    restrict(
        ["src/features/*/*/**/*.{ts,tsx}"],
        ...screens,
        "@/features/*/*/**",
        "../../*",
    ),
    restrict(["src/app/**/*.{ts,tsx}"], "@/components/**", "@/features/*/*/**"),
]);
