"use client";
import { createContext, useContext, useRef } from "react";

export const ChangeRequestUpdates = createContext(() => {});
export function useChangeRequestUpdates() {
    return useContext(ChangeRequestUpdates);
}

export function useProposalKey() {
    const attempt = useRef<{ payload: string; key: string } | null>(null);
    return {
        forPayload(payload: unknown) {
            const value = JSON.stringify(payload);
            if (attempt.current?.payload !== value)
                attempt.current = { payload: value, key: crypto.randomUUID() };
            return attempt.current.key;
        },
        reset() {
            attempt.current = null;
        },
    };
}
