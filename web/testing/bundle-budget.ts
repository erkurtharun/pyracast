import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

type ClientManifest = {
    entryJSFiles?: Record<string, string[]>;
    entryCSSFiles?: Record<string, { path: string }[]>;
};

export type Budget = {
    tolerancePercent: number;
    firstLoad: Record<string, number>;
    onDemand: Record<string, number>;
    otherOnDemand: number;
};
export type Measured = { name: string; bytes: number; limit: number };

const pageFolder = (dist: string, route: string) =>
    path.join(dist, "server", "app", route, "page");

// The scripts and stylesheets a modern browser loads for the route's first page: the shared
// runtime plus the client entries of its layouts and page. Polyfills load only in browsers
// without module support; metadata image routes are separate requests.
function firstLoadFiles(dist: string, route: string) {
    const page = pageFolder(dist, route);
    const build = JSON.parse(
        readFileSync(path.join(page, "build-manifest.json"), "utf8"),
    ) as { rootMainFiles: string[] };
    const source = readFileSync(`${page}_client-reference-manifest.js`, "utf8");
    const start = source.indexOf(
        `__RSC_MANIFEST[${JSON.stringify(`${route}/page`)}]`,
    );
    if (start < 0) throw new Error(`No client manifest for ${route}.`);
    const manifest = JSON.parse(
        source.slice(source.indexOf("=", start) + 1, source.lastIndexOf(";")),
    ) as ClientManifest;
    const entries = (record: Record<string, unknown[]> = {}) =>
        Object.entries(record)
            .filter(([entry]) => !entry.endsWith("--metadata"))
            .flatMap(([, files]) => files);
    return new Set([
        ...build.rootMainFiles,
        ...entries(manifest.entryJSFiles).map(String),
        ...entries(manifest.entryCSSFiles).map(
            (file) => (file as { path: string }).path,
        ),
    ]);
}

const bytes = (dist: string, files: Iterable<string>) =>
    [...files].reduce(
        (total, file) => total + statSync(path.join(dist, file)).size,
        0,
    );

export function routeBytes(dist: string, route: string) {
    return bytes(dist, firstLoadFiles(dist, route));
}

function pageRoutes(dist: string, folder = ""): string[] {
    const entries = readdirSync(path.join(dist, "server", "app", folder), {
        withFileTypes: true,
    });
    return entries.flatMap((entry) =>
        !entry.isDirectory()
            ? []
            : entry.name === "page"
              ? [folder]
              : pageRoutes(dist, `${folder}/${entry.name}`),
    );
}

function staticChunks(dist: string, folder = "static/chunks"): string[] {
    return readdirSync(path.join(dist, folder), {
        withFileTypes: true,
    }).flatMap((entry) => {
        const file = `${folder}/${entry.name}`;
        if (entry.isDirectory()) return staticChunks(dist, file);
        return /\.(js|css)$/.test(entry.name) ? [file] : [];
    });
}

export function budgetLimit(recorded: number, tolerancePercent: number) {
    return Math.floor((recorded * (100 + tolerancePercent)) / 100);
}

const literally = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// Turbopack registers a module's exports by name (`e.s(["Calibrations",0,...`), the one stable
// name an on-demand chunk carries; its file names and module ids change with every build. The name
// is looked for up to the registration's first `]`; a miss fails the measurement.
function exporting(dist: string, files: string[], name: string) {
    const marker = new RegExp(
        `[\\w$]\\.s\\(\\[(?:[^\\]]*?,)?${literally(JSON.stringify(name))},`,
    );
    const found = files.filter((file) =>
        marker.test(readFileSync(path.join(dist, file), "utf8")),
    );
    if (found.length !== 1)
        throw new Error(
            `Expected one on-demand chunk exporting ${name}, found ${found.length}.`,
        );
    return found;
}

// A chunk counts once: in a first load, else in the on-demand entry whose export it registers,
// else in the rest that no entry names (a shared on-demand chunk, a new on-demand import).
export function measure(dist: string, budget: Budget): Measured[] {
    const routes = pageRoutes(dist);
    const polyfills = new Set(
        routes.flatMap(
            (route) =>
                (
                    JSON.parse(
                        readFileSync(
                            path.join(
                                pageFolder(dist, route),
                                "build-manifest.json",
                            ),
                            "utf8",
                        ),
                    ) as { polyfillFiles: string[] }
                ).polyfillFiles,
        ),
    );
    const loaded = new Set(
        routes.flatMap((route) => [...firstLoadFiles(dist, route)]),
    );
    const onDemand = staticChunks(dist).filter(
        (file) => !loaded.has(file) && !polyfills.has(file),
    );
    const named = new Map<string, string>();
    const entry = (
        name: string,
        recorded: number,
        files: Iterable<string>,
    ) => ({
        name,
        bytes: bytes(dist, files),
        limit: budgetLimit(recorded, budget.tolerancePercent),
    });
    const measured = [
        ...Object.entries(budget.firstLoad).map(([route, recorded]) =>
            entry(route, recorded, firstLoadFiles(dist, route)),
        ),
        ...Object.entries(budget.onDemand).map(([name, recorded]) => {
            const files = exporting(dist, onDemand, name);
            for (const file of files) {
                const counted = named.get(file);
                if (counted)
                    throw new Error(
                        `${name} resolves to ${file}, which ${counted} already counts.`,
                    );
                named.set(file, name);
            }
            return entry(name, recorded, files);
        }),
    ];
    const rest = onDemand.filter((file) => !named.has(file));
    return [
        ...measured,
        entry("other on-demand chunks", budget.otherOnDemand, rest),
    ];
}
