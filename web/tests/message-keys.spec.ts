import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { keyUse, unusedKeys } from "./support/message-keys";
import { messages } from "../src/i18n/messages";

const web = path.resolve(__dirname, "..");
const flagProducers = {
    "Forecast.qualityFlags": [
        "web/src/flags.ts",
        "src/pyracast/forecasts/flags.py",
    ],
};

function tree(files: Record<string, string>, parent = tmpdir()): string {
    mkdirSync(parent, { recursive: true });
    const root = mkdtempSync(path.join(parent, "message-keys-"));
    for (const [name, text] of Object.entries(files)) {
        const file = path.join(root, name);
        mkdirSync(path.dirname(file), { recursive: true });
        writeFileSync(file, text);
    }
    return root;
}

test("every message key is read by a screen or names a code the server or a screen sends", () => {
    expect(unusedKeys(web, messages.en)).toEqual([]);
});

test("every translation call names its namespace and keys in a form the key check reads", () => {
    expect(keyUse(web).unresolved).toEqual([]);
});

test("a code named only in a comment, a docstring or a module that is not its producer labels nothing", () => {
    const root = tree({
        "web/src/flags.ts": [
            "declare function useCodeLabel(namespace: string): (code: string) => string;",
            "// Shown for 'provider_error'.",
            'export const label = useCodeLabel("Forecast.qualityFlags");',
            'export const state = "stale";',
        ].join("\n"),
        "src/pyracast/forecasts/flags.py": [
            '"""Flags such as "provider_error" are not produced here."""',
            "",
            "",
            "def flags(reason):",
            '    # "provider_error" was retired.',
            '    return ["#fff", "unvalidated", f"stale_{reason}"]',
        ].join("\n"),
        "src/pyracast/forecasts/migrations/0002_flags.py":
            'FLAGS = ["provider_error", "weather_request_failed"]\n',
    });
    try {
        expect(
            unusedKeys(
                path.join(root, "web"),
                {
                    Forecast: {
                        qualityFlags: {
                            provider_error: "provider error",
                            weather_request_failed: "weather request failed",
                            unvalidated: "unvalidated",
                            stale: "stale",
                            stale_source: "stale source",
                        },
                    },
                },
                flagProducers,
            ),
        ).toEqual([
            "Forecast.qualityFlags.provider_error",
            "Forecast.qualityFlags.weather_request_failed",
        ]);
    } finally {
        rmSync(root, { recursive: true, force: true });
    }
});

test("a checkout below a directory named migrations still reads the backend's codes", () => {
    const parent = mkdtempSync(path.join(tmpdir(), "message-keys-"));
    const root = tree(
        {
            "web/src/flags.ts": [
                "declare function useCodeLabel(namespace: string): (code: string) => string;",
                'export const label = useCodeLabel("Forecast.qualityFlags");',
            ].join("\n"),
            "src/pyracast/forecasts/flags.py": 'FLAGS = ["unvalidated"]\n',
        },
        path.join(parent, "migrations"),
    );
    try {
        expect(
            unusedKeys(
                path.join(root, "web"),
                {
                    Forecast: { qualityFlags: { unvalidated: "unvalidated" } },
                },
                flagProducers,
            ),
        ).toEqual([]);
    } finally {
        rmSync(parent, { recursive: true, force: true });
    }
});

test("a code a screen writes as a template literal without substitution labels its key", () => {
    const root = tree({
        "web/src/flags.ts": [
            "declare function useCodeLabel(namespace: string): (code: string) => string;",
            'export const label = useCodeLabel("Forecast.qualityFlags");',
            "export const state = `stale`;",
        ].join("\n"),
        "src/pyracast/forecasts/flags.py": "FLAGS = []\n",
    });
    try {
        expect(
            unusedKeys(
                path.join(root, "web"),
                {
                    Forecast: { qualityFlags: { stale: "stale" } },
                },
                flagProducers,
            ),
        ).toEqual([]);
    } finally {
        rmSync(root, { recursive: true, force: true });
    }
});

test("a code labels its key only where a module named as the family's producer holds it", () => {
    const root = tree({
        "web/src/flags.ts": [
            "declare function useCodeLabel(namespace: string): (code: string) => string;",
            'export const label = useCodeLabel("Calibrations.values");',
        ].join("\n"),
        "src/pyracast/plants/numerics.py": 'EXCLUSIONS = {"quality": 0}\n',
        "src/pyracast/plants/models.py": 'STATES = ["valid", "outage"]\n',
    });
    try {
        expect(
            unusedKeys(
                path.join(root, "web"),
                { Calibrations: { values: { quality: "q", valid: "v" } } },
                { "Calibrations.values": ["src/pyracast/plants/numerics.py"] },
            ),
        ).toEqual(["Calibrations.values.valid"]);
    } finally {
        rmSync(root, { recursive: true, force: true });
    }
});
