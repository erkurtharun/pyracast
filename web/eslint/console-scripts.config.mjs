import { generatedCode } from "./generated-code.mjs";

// The Django admin's browser scripts live outside this directory; the lint script runs this
// configuration from the repository root.
export default [
    {
        files: ["src/pyracast/regions/static/pyracast/*.js"],
        languageOptions: {
            sourceType: "script",
            globals: {
                setInterval: "readonly",
                setTimeout: "readonly",
                window: "readonly",
            },
        },
        linterOptions: { noInlineConfig: true },
        rules: generatedCode,
    },
];
