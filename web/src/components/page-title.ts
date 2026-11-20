"use client";
import { useEffect } from "react";

// The /app layout keeps one <title> mounted; a screen renames it while it is shown.
export function usePageTitle(title: string) {
    useEffect(() => {
        const previous = document.title;
        document.title = title;
        return () => {
            document.title = previous;
        };
    }, [title]);
}
