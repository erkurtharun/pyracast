"use client";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { api } from "@/lib/api/client";
import { routes } from "@/lib/routes";
import { sessionState, signOut, useSessionState } from "@/lib/session-client";
import { AsyncButton } from "@/ui/form";
import { InvitationDetails, type PreviewState } from "./invitation-preview";
import { clearWorkspaceInvitation } from "./workspace-invitation";

export function AcceptInvitation({
    invitationToken,
    preview,
}: {
    invitationToken: string;
    preview: PreviewState;
}) {
    const t = useTranslations("WorkspaceMembers");
    const { session } = useSessionState();
    const [accepting, setAccepting] = useState(false);
    const claimed = useRef(false);
    const claim = () => {
        if (claimed.current || sessionState().signOut.status !== "idle")
            return false;
        claimed.current = true;
        return true;
    };
    const accept = async () => {
        if (!claim()) return;
        setAccepting(true);
        try {
            await api.post("/api/invitations/accept/", {
                body: { token: invitationToken },
            });
        } catch (error) {
            claimed.current = false;
            setAccepting(false);
            throw error;
        }
        if (sessionState().signOut.status !== "idle") return;
        clearWorkspaceInvitation();
        window.location.replace(routes.workspaces());
    };
    const switchAccount = () => {
        if (claim()) void signOut({ keepInvitation: true }).catch(() => {});
    };
    return (
        <main className="auth-page">
            <section className="auth-card">
                <h1>{t("joinWorkspace")}</h1>
                <InvitationDetails state={preview} />
                <p>{t("signedInAs", { email: session?.user?.email ?? "" })}</p>
                {preview.status !== "unavailable" && (
                    <AsyncButton
                        permitted
                        errorTitle={t("acceptFailed")}
                        onClick={accept}
                    >
                        {t("accept")}
                    </AsyncButton>
                )}
                <div>
                    <button
                        type="button"
                        className="button secondary"
                        disabled={accepting}
                        onClick={switchAccount}
                    >
                        {t("useAnotherAccount")}
                    </button>
                </div>
                <button
                    type="button"
                    className="text-button"
                    onClick={() => {
                        clearWorkspaceInvitation();
                        window.location.replace(routes.workspaces());
                    }}
                >
                    {t("dismiss")}
                </button>
            </section>
        </main>
    );
}
