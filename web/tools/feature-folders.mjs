// Moves web/src/components into features/* and ui/* and rewrites every import to the new paths.
// Idempotent: a branch made before the move reruns it after a rebase (`node tools/feature-folders.mjs`).
import { spawnSync } from "node:child_process";
import {
    existsSync,
    mkdirSync,
    readdirSync,
    readFileSync,
    writeFileSync,
} from "node:fs";
import path from "node:path";

const web = path.resolve(import.meta.dirname, "..");
const src = path.join(web, "src");
const folders = {
    ui: [
        "brand.tsx",
        "csv-download.tsx",
        "language-select.tsx",
        "page-title.ts",
        "route-error.tsx",
        "scroll-region.tsx",
        "ui.tsx",
        "workspace-access.tsx",
    ],
    "features/session": [
        "app-root.tsx",
        "entry.tsx",
        "language-handoff.ts",
        "require-user.tsx",
        "session-language.tsx",
    ],
    "features/auth": ["auth.tsx", "social-provider-buttons.tsx"],
    "features/account": [
        "account-security.tsx",
        "account-settings.tsx",
        "authenticator-setup.tsx",
        "email-change.tsx",
        "linked-accounts.tsx",
        "recovery-codes.tsx",
        "security-reauthentication.tsx",
        "two-factor-settings.tsx",
    ],
    "features/workspace": [
        "invitation-entry.tsx",
        "workspace-invitation.ts",
        "workspace-members.tsx",
        "workspace-pages.tsx",
        "workspace-shell.tsx",
    ],
    "features/inbox": ["operational-inbox.tsx"],
    "features/trash": ["recycle-bin.tsx"],
    "features/portfolio": [
        "plant-configuration-fields.tsx",
        "plant-icon.tsx",
        "plant-onboarding.tsx",
        "portfolio.tsx",
    ],
    "features/plant": [
        "collection-schedule.tsx",
        "outages.tsx",
        "plant-site.tsx",
        "plant-workspace.tsx",
        "region-settings.tsx",
        "reminders.tsx",
    ],
    "features/calibration": ["calibration-automation.tsx", "calibrations.tsx"],
    "features/forecast": [
        "data-source.tsx",
        "forecast-intervals.tsx",
        "forecast-panel.tsx",
        "forecast-performance.tsx",
        "weather-reference.tsx",
    ],
    "features/production": ["production-history.tsx", "production-import.tsx"],
    "features/public-site": [
        "public-site/home-copy.ts",
        "public-site/home-page.tsx",
        "public-site/legal-copy.ts",
        "public-site/legal-page.tsx",
        "public-site/public-site.module.css",
        "public-site/site-shell.tsx",
        "public-site/social-image.tsx",
        "public-site/website-data.tsx",
    ],
};

const posix = (value) => value.split(path.sep).join("/");
const moves = new Map(
    Object.entries(folders).flatMap(([folder, files]) =>
        files.map((file) => [
            `components/${file}`,
            `${folder}/${path.posix.basename(file)}`,
        ]),
    ),
);
const moduleId = (file) =>
    file.endsWith(".css") ? file : file.replace(/\.tsx?$/, "");
const modules = new Map(
    [...moves].map(([from, to]) => [moduleId(from), moduleId(to)]),
);
const movedFrom = new Map([...moves].map(([from, to]) => [to, from]));

for (const [from, to] of moves) {
    if (!existsSync(path.join(src, from))) continue;
    mkdirSync(path.dirname(path.join(src, to)), { recursive: true });
    const result = spawnSync(
        "git",
        ["mv", path.join("src", from), path.join("src", to)],
        { cwd: web, stdio: "inherit", shell: false },
    );
    if (result.status !== 0) throw new Error(`git mv ${from} failed`);
}

function exists(id) {
    return ["", ".ts", ".tsx", "/index.ts", "/index.tsx"].some((suffix) =>
        existsSync(path.join(src, `${id}${suffix}`)),
    );
}

function sourceFiles(directory) {
    if (!existsSync(directory)) return [];
    return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
        const full = path.join(directory, entry.name);
        if (entry.isDirectory())
            return entry.name === "node_modules" ? [] : sourceFiles(full);
        return /\.(tsx?|mjs)$/.test(entry.name) ? [full] : [];
    });
}

function rewrite(file, specifier) {
    const inSrc = posix(path.relative(src, file));
    const insideSrc = !inSrc.startsWith("..");
    let target;
    if (specifier.startsWith("@/")) target = specifier.slice(2);
    else if (specifier.startsWith(".")) {
        const from = (directory) =>
            posix(path.relative(src, path.resolve(directory, specifier)));
        target = from(path.dirname(file));
        if (insideSrc && !exists(target) && movedFrom.has(inSrc))
            target = from(path.join(src, path.dirname(movedFrom.get(inSrc))));
    } else return specifier;
    const moved = modules.get(target);
    if (!moved) return specifier;
    if (!insideSrc)
        return posix(path.relative(path.dirname(file), path.join(src, moved)));
    return path.posix.dirname(moved) === path.posix.dirname(inSrc)
        ? `./${path.posix.basename(moved)}`
        : `@/${moved}`;
}

const pattern = /((?:from|import)\s*\(?\s*)"([^"]+)"/g;
for (const file of ["src", "tests", "integration", "testing"].flatMap(
    (directory) => sourceFiles(path.join(web, directory)),
)) {
    const text = readFileSync(file, "utf8");
    const next = text.replace(
        pattern,
        (match, prefix, specifier) => `${prefix}"${rewrite(file, specifier)}"`,
    );
    if (next !== text) writeFileSync(file, next);
}
