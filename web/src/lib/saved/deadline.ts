export type Deadline = {
    reached: Promise<void>;
    expired: () => boolean;
    clear: () => void;
};

// A background tab may fire the timer late; the elapsed time decides whether work may still act.
export function deadline(limitMs: number): Deadline {
    const admitted = performance.now();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const reached = new Promise<void>((resolve) => {
        timer = setTimeout(resolve, limitMs);
    });
    return {
        reached,
        expired: () => performance.now() - admitted >= limitMs,
        clear: () => clearTimeout(timer),
    };
}
