class SectionUnavailable extends Error {}

// The bundler runtime keeps a failed chunk failed for the life of the document (an offline moment,
// or a release that replaced the files), so only a reload loads the section again.
export function sectionUnavailable(cause: unknown): never {
    throw new SectionUnavailable("A screen section could not be loaded.", {
        cause,
    });
}

export function reloadPage() {
    window.location.reload();
}

export function reloadAfter(settled: Promise<unknown>, reload = reloadPage) {
    let active = true;
    void settled.then(() => {
        if (active) reload();
    });
    return () => {
        active = false;
    };
}

export function recovery(error: unknown, retry: () => void) {
    return error instanceof SectionUnavailable ? reloadPage : retry;
}
