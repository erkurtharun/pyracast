"use client";
import { createContext, useContext, useEffect } from "react";
import type { PlantDays } from "@/lib/format/time";

export const ShownDays = createContext<(days: PlantDays | undefined) => void>(
    () => undefined,
);

export function useShownDays(first?: string, last?: string) {
    const show = useContext(ShownDays);
    useEffect(() => {
        if (!first || !last) return;
        show([first, last]);
        return () => show(undefined);
    }, [show, first, last]);
}
