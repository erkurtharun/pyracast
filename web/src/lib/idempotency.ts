import { useState } from "react";
import { ApiError, UnconfirmedError } from "./api/errors";

// The characters Python's `str.strip()` removes, as the server strips texts before it compares or stores them.
const SPACE = new Set([
    0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0,
    0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007,
    0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
]);

export function serverTrim(text: string) {
    let start = 0;
    let end = text.length;
    while (start < end && SPACE.has(text.charCodeAt(start))) start++;
    while (end > start && SPACE.has(text.charCodeAt(end - 1))) end--;
    return text.slice(start, end);
}

export type Attempt<T = unknown> = { payload: string; key: string; value: T };

export function outstandingAfter<A extends { payload: string }>(
    outstanding: A | null,
    sent: A,
    error?: unknown,
): A | null {
    if (error instanceof UnconfirmedError) return sent;
    if (error instanceof ApiError && error.status === 429) return outstanding;
    return outstanding?.payload === sent.payload ? null : outstanding;
}

// One attempt per form: the request whose answer was lost, until an answer decides it.
export function useAttempt<T>() {
    const [outstanding, setOutstanding] = useState<Attempt<T> | null>(null);
    return {
        outstanding: outstanding?.value,
        repeats: (value: T) => outstanding?.payload === JSON.stringify(value),
        next(value: T): Attempt<T> {
            const payload = JSON.stringify(value);
            return outstanding?.payload === payload
                ? outstanding
                : { payload, key: crypto.randomUUID(), value };
        },
        settle(sent: Attempt<T>, error?: unknown) {
            setOutstanding((current) => outstandingAfter(current, sent, error));
        },
    };
}
