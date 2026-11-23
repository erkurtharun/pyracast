import { spawnSync } from "node:child_process";
import { readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";

const webDir = path.resolve(import.meta.dirname, "..");
const outputDir = path.join(webDir, "coverage");
if (path.dirname(outputDir) !== webDir)
    throw new Error("Invalid coverage directory");
const lock = path.join(webDir, "coverage.lock");
const args = process.argv.slice(2);

function alive(pid) {
    try {
        process.kill(pid, 0);
        return true;
    } catch (error) {
        return error.code === "EPERM";
    }
}

function holderOf() {
    try {
        return Number(readFileSync(lock, "utf8"));
    } catch {
        return 0;
    }
}

function claim() {
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
    if (claim()) return undefined;
    const holder = holderOf();
    if (holder > 0 && alive(holder))
        return `A coverage run (process ${holder}) holds this checkout; ${lock} is released when it ends.`;
    rmSync(lock, { force: true });
    return claim()
        ? undefined
        : `Another coverage run claimed this checkout first (${lock}).`;
}

const env = {
    ...process.env,
    PLAYWRIGHT_COVERAGE: "1",
    NEXT_DIST_DIR: ".next-coverage",
};
delete env.NODE_V8_COVERAGE;

function run(script, scriptArgs) {
    const result = spawnSync(process.execPath, [script, ...scriptArgs], {
        cwd: webDir,
        stdio: "inherit",
        env,
    });
    if (result.error) throw result.error;
    return result.status ?? 1;
}

const refused = refusal();
if (refused) {
    console.error(refused);
    process.exitCode = 2;
} else {
    try {
        rmSync(outputDir, { recursive: true, force: true });
        const built = run("node_modules/next/dist/bin/next", ["build"]);
        process.exitCode = built
            ? built
            : run("node_modules/@playwright/test/cli.js", ["test", ...args]);
    } finally {
        rmSync(lock, { force: true });
    }
}
