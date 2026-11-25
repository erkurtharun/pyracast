"use client";
import type { ReactNode } from "react";
import { useNavigate } from "@/lib/leave-guard";

// A link within the application: a plain primary click navigates within it, any other click keeps the browser's behaviour.
export function AppLink({
    href,
    className,
    label,
    children,
}: {
    href: string;
    className?: string;
    label?: string;
    children: ReactNode;
}) {
    const navigate = useNavigate();
    return (
        <a
            href={href}
            className={className}
            aria-label={label}
            onClick={(event) => {
                if (
                    event.defaultPrevented ||
                    event.button !== 0 ||
                    event.metaKey ||
                    event.ctrlKey ||
                    event.shiftKey ||
                    event.altKey
                )
                    return;
                event.preventDefault();
                navigate(href);
            }}
        >
            {children}
        </a>
    );
}
