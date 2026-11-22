import { useSyncExternalStore } from "react";

type InstallPromptEvent = Event & {
    prompt: () => Promise<unknown>;
    userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
};
type Prompt =
    | { kind: "none" }
    | { kind: "saved"; event: InstallPromptEvent }
    | { kind: "installed" };
type InstallSteps = "browserMenu" | "homeScreen" | "dock";
type InstallOption =
    | { kind: "unavailable" }
    | { kind: "prompt"; event: InstallPromptEvent }
    | { kind: "steps"; steps: InstallSteps };

const standaloneQuery = "(display-mode: standalone)";
const unavailable = { kind: "unavailable" } as const;
let prompt: Prompt = { kind: "none" };
const listeners = new Set<() => void>();

function setPrompt(next: Prompt) {
    prompt = next;
    for (const listener of listeners) listener();
}

function isInstallPrompt(event: Event): event is InstallPromptEvent {
    return "prompt" in event && "userChoice" in event;
}

export function captureInstallPrompt() {
    window.addEventListener("beforeinstallprompt", (event) => {
        if (!isInstallPrompt(event)) return;
        event.preventDefault();
        setPrompt({ kind: "saved", event });
    });
    window.addEventListener("appinstalled", () =>
        setPrompt({ kind: "installed" }),
    );
}

export async function promptInstall(event: InstallPromptEvent) {
    if (prompt.kind !== "saved" || prompt.event !== event) return;
    setPrompt({ kind: "none" });
    await event.prompt();
    const choice = await event.userChoice;
    if (choice.outcome === "accepted") setPrompt({ kind: "installed" });
}

function subscribePrompt(listener: () => void) {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

function subscribeStandalone(listener: () => void) {
    const query = window.matchMedia(standaloneQuery);
    query.addEventListener("change", listener);
    return () => query.removeEventListener("change", listener);
}

function subscribeNothing() {
    return () => {};
}

function platformSteps(): InstallSteps | null {
    if ("onbeforeinstallprompt" in window) return "browserMenu";
    if (!("GestureEvent" in window)) return null;
    return navigator.maxTouchPoints > 0 ? "homeScreen" : "dock";
}

function installOption(
    standalone: boolean,
    current: Prompt,
    steps: InstallSteps | null,
): InstallOption {
    if (standalone || current.kind === "installed") return unavailable;
    if (current.kind === "saved")
        return { kind: "prompt", event: current.event };
    return steps ? { kind: "steps", steps } : unavailable;
}

export function useInstallOption() {
    const current = useSyncExternalStore(
        subscribePrompt,
        () => prompt,
        () => prompt,
    );
    const standalone = useSyncExternalStore(
        subscribeStandalone,
        () => window.matchMedia(standaloneQuery).matches,
        () => true,
    );
    const steps = useSyncExternalStore(
        subscribeNothing,
        platformSteps,
        () => null,
    );
    return installOption(standalone, current, steps);
}
