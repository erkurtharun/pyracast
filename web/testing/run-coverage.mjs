import { spawnSync } from "node:child_process";
import { rmSync } from "node:fs";
import path from "node:path";

const webDir = path.resolve(import.meta.dirname, "..");
const outputDir = path.join(webDir, "coverage");
if (path.dirname(outputDir) !== webDir)
    throw new Error("Invalid coverage directory");
rmSync(outputDir, { recursive: true, force: true });

const result = spawnSync(
    process.execPath,
    ["node_modules/@playwright/test/cli.js", "test", ...process.argv.slice(2)],
    {
        cwd: webDir,
        stdio: "inherit",
        env: {
            ...process.env,
            PLAYWRIGHT_COVERAGE: "1",
            NEXT_DIST_DIR: ".next-coverage",
        },
    },
);
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
