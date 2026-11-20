"use client";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { routes } from "@/lib/routes";
import { useSessionState } from "@/lib/session-client";
import { RequireUser } from "@/features/session/require-user";
import {
    storedInvitation,
    storeInvitation,
    useStoredInvitation,
} from "./workspace-invitation";
import { AcceptInvitation } from "./accept-invitation";

function takeFragmentToken() {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const token = fragment.get("token");
    if (token === null) return;
    storeInvitation(token);
    window.history.replaceState(
        window.history.state,
        "",
        `${window.location.pathname}${window.location.search}`,
    );
}

export function InvitationEntry() {
    const router = useRouter();
    const token = useStoredInvitation();
    const signedIn = Boolean(useSessionState().session?.user);
    useEffect(takeFragmentToken, []);
    useEffect(() => {
        if (token === null && signedIn && !storedInvitation())
            router.replace(routes.workspaces());
    }, [token, signedIn, router]);
    return (
        <RequireUser>
            {token && <AcceptInvitation invitationToken={token} />}
        </RequireUser>
    );
}
