"use client";
import {
    createContext,
    useCallback,
    useContext,
    useState,
    type ReactNode,
} from "react";

const Announce = createContext<(message: string) => void>(() => {});

// Mounted once and empty before the first message, so assistive technology already watches it when a message arrives.
// Each message replaces the node, so a repeated message is spoken again.
export function LiveRegion({ children }: { children: ReactNode }) {
    const [message, setMessage] = useState({ text: "", count: 0 });
    const announce = useCallback(
        (text: string) =>
            setMessage((current) => ({ text, count: current.count + 1 })),
        [],
    );
    return (
        <Announce value={announce}>
            {children}
            <div
                className="sr-only"
                data-live-region
                aria-live="polite"
                aria-atomic="true"
            >
                <span key={message.count}>{message.text}</span>
            </div>
        </Announce>
    );
}

export function useAnnounce() {
    return useContext(Announce);
}
