import { useCallback, useRef, useState } from "react";

// One action of a panel at a time: while one runs, the others neither start nor can be pressed.
export function useOneAction() {
    const running = useRef(false);
    const [acting, setActing] = useState(false);
    const run = useCallback(async (action: () => Promise<void>) => {
        if (running.current) return;
        running.current = true;
        setActing(true);
        try {
            await action();
        } finally {
            running.current = false;
            setActing(false);
        }
    }, []);
    return { acting, run };
}
