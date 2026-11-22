import { expect, test } from "@playwright/test";
import { captureInstallPrompt, promptInstall } from "../src/lib/install-prompt";

type Outcome = "accepted" | "dismissed";
type InstallEvent = Parameters<typeof promptInstall>[0] & { prompts: number };

const realWindow = globalThis.window;
let target: EventTarget;

function installEvent(outcome: Outcome): InstallEvent {
    const event = Object.assign(
        new Event("beforeinstallprompt", { cancelable: true }),
        {
            prompts: 0,
            prompt: () => {
                event.prompts += 1;
                return Promise.resolve();
            },
            userChoice: Promise.resolve({ outcome }),
        },
    );
    return event;
}

function offer(outcome: Outcome = "accepted") {
    const event = installEvent(outcome);
    target.dispatchEvent(event);
    return event;
}

test.beforeEach(() => {
    target = new EventTarget();
    Object.assign(globalThis, { window: target });
    captureInstallPrompt();
});
test.afterEach(() => {
    Object.assign(globalThis, { window: realWindow });
});

test("a saved install event prompts only once when it is used twice", async () => {
    const event = offer("dismissed");
    await promptInstall(event);
    await promptInstall(event);
    expect(event.prompts).toBe(1);
});

test("a replaced install event no longer prompts and leaves the newer one saved", async () => {
    const first = offer();
    const second = offer();
    await promptInstall(first);
    await promptInstall(second);
    expect([first.prompts, second.prompts]).toEqual([0, 1]);
});

test("a dismissed install event is not offered again", async () => {
    const event = offer("dismissed");
    await promptInstall(event);
    const next = offer();
    await promptInstall(event);
    await promptInstall(next);
    expect([event.prompts, next.prompts]).toEqual([1, 1]);
});

test("an installed app no longer prompts with the saved event", async () => {
    const event = offer();
    target.dispatchEvent(new Event("appinstalled"));
    await promptInstall(event);
    expect(event.prompts).toBe(0);
});
