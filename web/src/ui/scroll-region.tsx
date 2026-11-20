"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

export function ScrollRegion({
    label,
    children,
}: {
    label: string;
    children: ReactNode;
}) {
    const container = useRef<HTMLDivElement>(null);
    const [overflowing, setOverflowing] = useState(true);

    useEffect(() => {
        const element = container.current;
        if (!element) return;
        const observer = new ResizeObserver(() => {
            setOverflowing(
                element.clientWidth > 0 &&
                    element.scrollWidth > element.clientWidth,
            );
        });
        observer.observe(element);
        const table = element.querySelector(":scope > table");
        if (table) observer.observe(table);
        return () => observer.disconnect();
    }, [children]);

    return (
        <div
            ref={container}
            className="table-scroll"
            role={overflowing ? "region" : undefined}
            aria-label={overflowing ? label : undefined}
            tabIndex={overflowing ? 0 : undefined}
        >
            {children}
        </div>
    );
}
