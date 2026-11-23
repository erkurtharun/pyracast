import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { rmSync } from "node:fs";
import path from "node:path";
import {
    coverageLockPort,
    coverageRunVariable,
} from "../tests/support/server.ts";
import { holdCoverageLock } from "./coverage-lock.mjs";

const webDir = path.resolve(import.meta.dirname, "..");
const outputDir = path.join(webDir, "coverage");
if (path.dirname(outputDir) !== webDir)
    throw new Error("Invalid coverage directory");
const args = process.argv.slice(2);

function run(script, scriptArgs, env) {
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

if (args.some((arg) => /^--ui($|=|-)/.test(arg))) {
    console.error(
        "The coverage run refuses Playwright UI mode: it collects the server's coverage and reports once, after the whole run.",
    );
    process.exitCode = 2;
} else if (!(await holdCoverageLock(coverageLockPort))) {
    console.error(
        `Port ${coverageLockPort} on 127.0.0.1 is taken: another coverage run on this machine holds it, and the coverage server's ports are shared by all of them.`,
    );
    process.exitCode = 2;
} else {
    const env = {
        ...process.env,
        PLAYWRIGHT_COVERAGE: "1",
        NEXT_DIST_DIR: ".next-coverage",
        [coverageRunVariable]: randomUUID(),
    };
    delete env.NODE_V8_COVERAGE;
    rmSync(outputDir, { recursive: true, force: true });
    const built = await run("node_modules/next/dist/bin/next", ["build"], env);
    process.exitCode =
        built ||
        (await run(
            "node_modules/@playwright/test/cli.js",
            ["test", ...args],
            env,
        ));
}
