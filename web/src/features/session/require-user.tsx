"use client";
import { useSearchParams } from "next/navigation";
import { Fragment, type ReactNode } from "react";
import { appQuery } from "@/lib/routes";
import { sessionOwner, useSessionState } from "@/lib/session-client";
import { Auth } from "@/features/auth/auth";
import { useStoredInvitation } from "@/features/workspace/workspace-invitation";
import { LaunchCard } from "./app-root";

export function RequireUser({
    children,
    invitationToken,
}: {
    children: ReactNode;
    invitationToken?: string | null;
}) {
    const { signup } = appQuery(useSearchParams());
    const { session } = useSessionState();
    const stored = useStoredInvitation();
    if (session?.user)
        return <Fragment key={sessionOwner(session)}>{children}</Fragment>;
    if (stored === undefined) return <LaunchCard />;
    return (
        <Auth
            invitationToken={invitationToken ?? stored}
            initialMode={signup ? "register" : "login"}
        />
    );
}
