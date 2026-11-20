"use client";
import { createContext, useContext, useState, type ReactNode } from "react";

const Announce = createContext<(message: string) => void>(() => {});

// Mounted once and empty before the first message, so assistive technology already watches it when a message arrives.
export function LiveRegion({ children }: { children: ReactNode }) {
    const [message, setMessage] = useState("");
    return (
        <Announce value={setMessage}>
            {children}
            <div
                className="sr-only"
                data-live-region
                aria-live="polite"
                aria-atomic="true"
            >
                {message}
            </div>
        </Announce>
    );
}

export function useAnnounce() {
    return useContext(Announce);
}
