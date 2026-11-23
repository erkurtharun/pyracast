import { spawn } from "node:child_process";
import { rmSync, writeFileSync } from "node:fs";
import { constants } from "node:os";
import path from "node:path";

const webDir = path.resolve(import.meta.dirname, "..");
const outputDir = path.join(webDir, "coverage");
if (path.dirname(outputDir) !== webDir)
    throw new Error("Invalid coverage directory");
const lock = path.join(webDir, "coverage.lock");
const args = process.argv.slice(2);

function claimed() {
    try {
        writeFileSync(lock, String(process.pid), { flag: "wx" });
        return true;
    } catch (error) {
        if (error.code !== "EEXIST") throw error;
        return false;
    }
}

function refusal() {
    if (args.some((arg) => /^--ui($|=|-)/.test(arg)))
        return "The coverage run refuses Playwright UI mode: it collects the server's coverage and reports once, after the whole run.";
    if (claimed()) return undefined;
    return `${lock} exists, so another coverage run may hold this checkout. Delete it by hand only after making sure that no coverage run of this checkout (this wrapper, its build, Playwright or the coverage server) is running.`;
}

const env = {
    ...process.env,
    PLAYWRIGHT_COVERAGE: "1",
    NEXT_DIST_DIR: ".next-coverage",
};
delete env.NODE_V8_COVERAGE;

let interrupted;

function run(script, scriptArgs) {
    if (interrupted) return Promise.resolve(1);
    return new Promise((resolve, reject) => {
        spawn(process.execPath, [script, ...scriptArgs], {
            cwd: webDir,
            stdio: "inherit",
            env,
        })
            .on("error", reject)
            .on("exit", (code) => resolve(code ?? 1));
    });
}

const refused = refusal();
if (refused) {
    console.error(refused);
    process.exitCode = 2;
} else {
    // The terminal delivers an interrupt to the running child itself; the wrapper waits for it and releases the lock.
    for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"])
        process.on(signal, () => {
            interrupted ??= signal;
        });
    try {
        rmSync(outputDir, { recursive: true, force: true });
        const built = await run("node_modules/next/dist/bin/next", ["build"]);
        process.exitCode = built
            ? built
            : await run("node_modules/@playwright/test/cli.js", [
                  "test",
                  ...args,
              ]);
        if (interrupted)
            process.exitCode = 128 + constants.signals[interrupted];
    } finally {
        rmSync(lock, { force: true });
    }
}
