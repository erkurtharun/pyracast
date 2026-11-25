let unanswered = 0;
let changed = () => {};

export function trackWrite<T>(answer: Promise<T>): Promise<T> {
    count(1);
    const settle = () => count(-1);
    answer.then(settle, settle);
    return answer;
}

function count(step: number) {
    unanswered += step;
    changed();
}

export function writesUnanswered() {
    return unanswered > 0;
}

export function whenWritesChange(listener: () => void) {
    changed = listener;
}
