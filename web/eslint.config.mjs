import { defineConfig, globalIgnores } from "eslint/config";
import js from "@eslint/js";
import ts from "typescript-eslint";
import next from "@next/eslint-plugin-next";
import hooks from "eslint-plugin-react-hooks";
import a11y from "eslint-plugin-jsx-a11y-x";
import css from "@eslint/css";
import pyracast from "./eslint/design-tokens.mjs";
import { generatedCode } from "./eslint/generated-code.mjs";

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
const screens = ["**/app/**", "**/components/**"];
// The type-checked rules of typescript-eslint's recommended set, without its no-unsafe-* family.
const typeAware = Object.fromEntries(
    [
        "await-thenable",
        "no-array-delete",
        "no-base-to-string",
        "no-duplicate-type-constituents",
        "no-floating-promises",
        "no-for-in-array",
        "no-implied-eval",
        "no-misused-promises",
        "no-redundant-type-constituents",
        "no-unnecessary-type-assertion",
        "no-unsafe-enum-comparison",
        "no-unsafe-unary-minus",
        "only-throw-error",
        "prefer-promise-reject-errors",
        "require-await",
        "restrict-plus-operands",
        "restrict-template-expressions",
        "unbound-method",
    ].map((rule) => [`@typescript-eslint/${rule}`, "error"]),
);
export default defineConfig([
    globalIgnores([
        ".next/**",
        ".next-*/**",
        "src/lib/schema.d.ts",
        "playwright-report/**",
        "test-results/**",
        "integration-results/**",
        "production-results/**",
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
            a11y.configs.recommended,
        ],
        languageOptions: {
            parserOptions: {
                projectService: true,
                tsconfigRootDir: import.meta.dirname,
            },
        },
        plugins: { "@next/next": next },
        rules: {
            ...next.configs.recommended.rules,
            ...next.configs["core-web-vitals"].rules,
            ...typeAware,
            "jsx-a11y-x/no-autofocus": ["error", { ignoreNonDOM: true }],
            "no-eval": "error",
        },
    },
    {
        files: ["**/*.mjs"],
        extends: [js.configs.recommended],
        languageOptions: {
            globals: {
                console: "readonly",
                process: "readonly",
                URL: "readonly",
            },
        },
        rules: generatedCode,
    },
    restrict(
        ["src/lib/**/*.{ts,tsx}", "src/i18n/**/*.{ts,tsx}"],
        ...screens,
        "**/features/**",
        "**/ui/**",
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
    restrict(
        ["src/app/**/*.{ts,tsx}"],
        "**/components/**",
        "**/features/*/*/**",
    ),
    {
        files: ["**/*.css"],
        language: "css/css",
        plugins: { css },
    },
    {
        files: ["src/**/*.{ts,tsx,css}"],
        plugins: { pyracast },
        rules: { "pyracast/design-tokens": "error" },
    },
    {
        files: ["src/styles/tokens.{ts,css}"],
        rules: { "pyracast/design-tokens": "off" },
    },
    // Inline directives could switch a rule off; no source file needs one.
    { linterOptions: { noInlineConfig: true } },
]);
