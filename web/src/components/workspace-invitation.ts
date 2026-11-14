"use client";
import { useEffect, useState } from "react";

const storageKey = "pyracast.workspaceInvitation";
export function clearWorkspaceInvitation() {
    try {
        window.sessionStorage.removeItem(storageKey);
    } catch {
        // Browser privacy settings can disable session storage.
    }
}
export function useWorkspaceInvitation() {
    const [invitation, setInvitation] = useState<
        { ready: false } | { ready: true; token: string | null }
    >({ ready: false });
    useEffect(() => {
        const restore = () => {
            const url = new URL(window.location.href);
            const fragment = new URLSearchParams(url.hash.slice(1));
            const fragmentToken = fragment.get("token");
            let token = fragmentToken ?? url.searchParams.get("token");
            try {
                if (token) window.sessionStorage.setItem(storageKey, token);
                else token = window.sessionStorage.getItem(storageKey);
            } catch {
                // Browser privacy settings can disable session storage.
            }
            if (fragmentToken !== null || url.searchParams.has("token")) {
                url.searchParams.delete("token");
                if (fragmentToken !== null) {
                    fragment.delete("token");
                    url.hash = fragment.toString();
                }
                window.history.replaceState(
                    window.history.state,
                    "",
                    `${url.pathname}${url.search}${url.hash}`,
                );
            }
            setInvitation({
                ready: true,
                token:
                    url.searchParams.get("account") === "security"
                        ? null
                        : token,
            });
        };
        restore();
    }, []);
    return invitation;
}
