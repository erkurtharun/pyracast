"use client";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";

// A link within the application: a plain primary click navigates through the router, any other click keeps the browser's behaviour.
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
    const router = useRouter();
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
                router.push(href);
            }}
        >
            {children}
        </a>
    );
}
