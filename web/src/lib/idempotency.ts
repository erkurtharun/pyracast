import { useState } from "react";
import { ApiError, UnconfirmedError } from "./api/errors";

export type Attempt = { payload: string; key: string };

export function outstandingAfter<A extends { payload: string }>(
    outstanding: A | null,
    sent: A,
    error?: unknown,
): A | null {
    if (error instanceof UnconfirmedError) return sent;
    if (error instanceof ApiError && error.status === 429) return outstanding;
    return outstanding?.payload === sent.payload ? null : outstanding;
}

export function useAttempt() {
    const [outstanding, setOutstanding] = useState<Attempt | null>(null);
    return {
        repeats: (payload: unknown) =>
            outstanding?.payload === JSON.stringify(payload),
        next(payload: unknown): Attempt {
            const value = JSON.stringify(payload);
            return outstanding?.payload === value
                ? outstanding
                : { payload: value, key: crypto.randomUUID() };
        },
        settle(sent: Attempt, error?: unknown) {
            setOutstanding((current) => outstandingAfter(current, sent, error));
        },
    };
}
