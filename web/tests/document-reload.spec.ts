import { expect, test } from "@playwright/test";
import { reloadAfter } from "../src/lib/on-demand";

function pending() {
    let settle!: () => void;
    const settled = new Promise<void>((resolve) => (settle = resolve));
    return { settled, settle };
}

test("a reload waits until the answers settle and then runs once", async () => {
    const { settled, settle } = pending();
    let reloads = 0;
    reloadAfter(settled, () => (reloads += 1));
    await Promise.resolve();
    expect(reloads).toBe(0);
    settle();
    await settled;
    expect(reloads).toBe(1);
});

test("a reload cleaned up before the answers settle does not run, and one set up again runs once", async () => {
    const { settled, settle } = pending();
    let reloads = 0;
    const cleanUp = reloadAfter(settled, () => (reloads += 1));
    cleanUp();
    reloadAfter(settled, () => (reloads += 1));
    settle();
    await settled;
    expect(reloads).toBe(1);
});
