"use client";
import { useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { Fragment, type ReactNode } from "react";
import { appQuery } from "@/lib/routes";
import { useSessionState } from "@/lib/session-client";
import { Auth } from "@/features/auth/auth";
import { useStoredInvitation } from "@/features/workspace/workspace-invitation";

export function RequireUser({
    children,
    invitationToken,
}: {
    children: ReactNode;
    invitationToken?: string | null;
}) {
    const t = useTranslations("Dashboard");
    const { signup } = appQuery(useSearchParams());
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
        <Auth
            invitationToken={invitationToken ?? stored}
            initialMode={signup ? "register" : "login"}
        />
    );
}
