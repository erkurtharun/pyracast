import { readFileSync, statSync } from "node:fs";
import path from "node:path";

type ClientManifest = {
    entryJSFiles?: Record<string, string[]>;
    entryCSSFiles?: Record<string, { path: string }[]>;
};

// Bytes on disk of the scripts and stylesheets a modern browser loads for the route's first
// page: the shared runtime plus the client entries of its layouts and page. Polyfills load only
// in browsers without module support; metadata image routes are separate requests.
export function routeBytes(dist: string, route: string) {
    const page = path.join(dist, "server", "app", route, "page");
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
    const files = new Set([
        ...build.rootMainFiles,
        ...entries(manifest.entryJSFiles).map(String),
        ...entries(manifest.entryCSSFiles).map(
            (file) => (file as { path: string }).path,
        ),
    ]);
    return [...files].reduce(
        (total, file) => total + statSync(path.join(dist, file)).size,
        0,
    );
}

export function budgetLimit(baseline: number) {
    return Math.floor(baseline * 1.05);
}
