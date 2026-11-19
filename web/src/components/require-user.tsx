"use client";
import { useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { Fragment, type ReactNode } from "react";
import { useSessionState } from "@/lib/session-client";
import { Auth } from "./auth";
import { WorkspaceAccess } from "./workspace-access";
import { useStoredInvitation } from "./workspace-invitation";

export function RequireUser({
    children,
    invitationToken,
}: {
    children: ReactNode;
    invitationToken?: string | null;
}) {
    const t = useTranslations("Dashboard");
    const signup = useSearchParams().get("signup") === "1";
    const { session } = useSessionState();
    const stored = useStoredInvitation();
    if (session?.user)
        return <Fragment key={session.user.id}>{children}</Fragment>;
    if (stored === undefined)
        return (
            <main className="auth-page">
                <p role="status">{t("loadingWorkspaces")}</p>
            </main>
        );
    return (
        <WorkspaceAccess value={true}>
            <Auth
                invitationToken={invitationToken ?? stored}
                initialMode={signup ? "register" : "login"}
            />
        </WorkspaceAccess>
    );
}
