import type { Page } from "@playwright/test";

export const installedModes = [
    "standalone",
    "minimal-ui",
    "window-controls-overlay",
] as const;
export type DisplayMode = "browser" | (typeof installedModes)[number];

type StandIn = { setDisplayMode: (mode: DisplayMode) => void };

export function standInDisplayMode(page: Page, initial: DisplayMode) {
    return page.addInitScript(
        ({ modes, initial }) => {
            const media = window.matchMedia.bind(window);
            let current = initial;
            const lists = new Map(
                modes.map((mode) => {
                    const query = `(display-mode: ${mode})`;
                    const list = media(query);
                    Object.defineProperty(list, "matches", {
                        get: () => current === mode,
                    });
                    return [query, { mode, list }] as const;
                }),
            );
            window.matchMedia = (asked) =>
                lists.get(asked)?.list ?? media(asked);
            const standIn: StandIn = {
                setDisplayMode(next) {
                    const previous = current;
                    current = next;
                    for (const [query, { mode, list }] of lists)
                        if (mode === previous || mode === next)
                            list.dispatchEvent(
                                new MediaQueryListEvent("change", {
                                    matches: mode === next,
                                    media: query,
                                }),
                            );
                },
            };
            Object.assign(window, standIn);
        },
        { modes: ["browser", ...installedModes], initial },
    );
}

export function setDisplayMode(page: Page, mode: DisplayMode) {
    return page.evaluate(
        (next) => (window as unknown as StandIn).setDisplayMode(next),
        mode,
    );
}
