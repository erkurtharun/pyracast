import type { Page } from "@playwright/test";

const standaloneQuery = "(display-mode: standalone)";

type StandIn = { setStandalone: (standalone: boolean) => void };

export function standInDisplayMode(page: Page, standalone: boolean) {
    return page.addInitScript(
        ({ query, initial }) => {
            const media = window.matchMedia.bind(window);
            const list = media(query);
            let matches = initial;
            Object.defineProperty(list, "matches", { get: () => matches });
            window.matchMedia = (asked) =>
                asked === query ? list : media(asked);
            const standIn: StandIn = {
                setStandalone(next) {
                    matches = next;
                    list.dispatchEvent(
                        new MediaQueryListEvent("change", {
                            matches: next,
                            media: query,
                        }),
                    );
                },
            };
            Object.assign(window, standIn);
        },
        { query: standaloneQuery, initial: standalone },
    );
}

export function setStandalone(page: Page, standalone: boolean) {
    return page.evaluate(
        (next) => (window as unknown as StandIn).setStandalone(next),
        standalone,
    );
}
