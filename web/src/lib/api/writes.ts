let started = 0;
let confirmed = 0;
const unanswered = new Set<number>();

export function trackWrite<T>(answer: Promise<T>): Promise<T> {
    const serial = ++started;
    unanswered.add(serial);
    answer.then(
        () => {
            confirmed = Math.max(confirmed, serial);
            unanswered.delete(serial);
        },
        () => unanswered.delete(serial),
    );
    return answer;
}

export function writesStarted() {
    return started;
}

export function confirmedSince(since: number) {
    return (
        confirmed > since && [...unanswered].every((serial) => serial <= since)
    );
}
