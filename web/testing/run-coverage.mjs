import { spawnSync } from "node:child_process";
import { rmSync } from "node:fs";
import path from "node:path";

const webDir = path.resolve(import.meta.dirname, "..");
const outputDir = path.join(webDir, "coverage");
if (path.dirname(outputDir) !== webDir)
    throw new Error("Invalid coverage directory");
rmSync(outputDir, { recursive: true, force: true });

const env = {
    ...process.env,
    PLAYWRIGHT_COVERAGE: "1",
    NEXT_DIST_DIR: ".next-coverage",
};
function run(script, args) {
    const result = spawnSync(process.execPath, [script, ...args], {
        cwd: webDir,
        stdio: "inherit",
        env,
    });
    if (result.error) throw result.error;
    return result.status ?? 1;
}

const built = run("node_modules/next/dist/bin/next", ["build"]);
process.exitCode = built
    ? built
    : run("node_modules/@playwright/test/cli.js", [
          "test",
          ...process.argv.slice(2),
      ]);
