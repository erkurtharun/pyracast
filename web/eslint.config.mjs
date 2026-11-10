import { defineConfig, globalIgnores } from "eslint/config";
import js from "@eslint/js";
import ts from "typescript-eslint";
import next from "@next/eslint-plugin-next";
import hooks from "eslint-plugin-react-hooks";
export default defineConfig([
    globalIgnores([
        ".next/**",
        ".next-*/**",
        "src/lib/schema.d.ts",
        "playwright-report/**",
        "test-results/**",
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
]);
