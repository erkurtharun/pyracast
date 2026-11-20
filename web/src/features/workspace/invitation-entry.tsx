"use client";
import { useTranslations } from "next-intl";
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
import { InvitationDetails, useInvitationPreview } from "./invitation-preview";

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
    const t = useTranslations("WorkspaceMembers");
    const router = useRouter();
    const token = useStoredInvitation();
    const account = useSessionState().session?.user?.id;
    const signedIn = account !== undefined;
    const preview = useInvitationPreview(token, account);
    useEffect(takeFragmentToken, []);
    useEffect(() => {
        if (token === null && signedIn && !storedInvitation())
            router.replace(routes.workspaces());
    }, [token, signedIn, router]);
    return (
        <>
            {token && !signedIn && (
                <section
                    className="invitation-banner"
                    aria-label={t("invitation")}
                >
                    <InvitationDetails state={preview} />
                    {preview.status === "ready" && (
                        <p>
                            {t("signInToJoin", {
                                email: preview.preview.email,
                            })}
                        </p>
                    )}
                </section>
            )}
            <RequireUser>
                {token && (
                    <AcceptInvitation
                        invitationToken={token}
                        preview={preview}
                    />
                )}
            </RequireUser>
        </>
    );
}
