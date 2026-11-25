"use client";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useNavigating } from "./workspace-shell";

// A later replace discards a pending push, so a redirect yields to the shell navigation the
// person started; the transition stays pending until the router commits or drops that push,
// which holds only while the shell starts the push synchronously inside the transition.
export function Replace({ href }: { href: string }) {
    const router = useRouter();
    const navigating = useNavigating();
    useEffect(() => {
        if (!navigating) router.replace(href);
    }, [navigating, router, href]);
    return null;
}
