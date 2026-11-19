// A random value the tabs of this browser share; a fresh one means the browser's session changed.
// It names no account. Without readable storage it stays null, so every tab keeps one generation.
export const generationKey = "pyracast.sessionGeneration";
export type Generation = string | null;

function storage() {
    try {
        return typeof window === "undefined" ? undefined : window.localStorage;
    } catch {
        return undefined;
    }
}

export function storedGeneration(): Generation {
    try {
        return storage()?.getItem(generationKey) ?? null;
    } catch {
        return null;
    }
}

export function writeGeneration() {
    const value = Array.from(
        crypto.getRandomValues(new Uint32Array(4)),
        (part) => part.toString(36),
    ).join("-");
    try {
        storage()?.setItem(generationKey, value);
    } catch {
        // Blocked storage leaves the generation unchanged, as on a browser without it.
    }
}
