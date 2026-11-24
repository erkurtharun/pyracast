import { useEffect, useId, useRef } from "react";

export function useRowFocus(rows: { id: number }[]) {
    const prefix = useId();
    const pending = useRef<number | null>(null);
    useEffect(() => {
        if (pending.current !== null) {
            document.getElementById(`${prefix}-${pending.current}`)?.focus();
            pending.current = null;
        }
    }, [prefix, rows]);
    return {
        prefix,
        focusRow: (id: number) => {
            pending.current = id;
        },
    };
}
