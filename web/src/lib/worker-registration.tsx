"use client";

import { useEffect } from "react";

export function WorkerRegistration() {
    useEffect(() => {
        if (process.env.NODE_ENV !== "production") return;
        if (!("serviceWorker" in navigator)) return;
        navigator.serviceWorker
            .register("/app/sw.js", { scope: "/app/", updateViaCache: "none" })
            .catch(() => undefined);
    }, []);
    return null;
}
