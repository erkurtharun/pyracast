"use client";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api/client";
import { authRequest } from "@/lib/auth-api";
import { routes } from "@/lib/routes";
import { useSessionState } from "@/lib/session-client";
import { ActionForm } from "@/ui/ui";
import { clearWorkspaceInvitation } from "./workspace-invitation";

export function AcceptInvitation({
    invitationToken,
}: {
    invitationToken: string;
}) {
    const t = useTranslations("WorkspaceMembers");
    const { session } = useSessionState();
    return (
        <main className="auth-page">
            <section className="auth-card">
                <h1>{t("joinWorkspace")}</h1>
                <p>{session?.user?.email}</p>
                <ActionForm
                    children={null}
                    submit={t("accept")}
                    onSubmit={async () => {
                        await api.post("/api/invitations/accept/", {
                            body: { token: invitationToken },
                        });
                        clearWorkspaceInvitation();
                        window.location.replace(routes.workspaces());
                    }}
                />
                <ActionForm
                    children={null}
                    submit={t("useAnotherAccount")}
                    onSubmit={async () => {
                        await authRequest("auth/session", "DELETE");
                        window.location.replace(routes.invitation());
                    }}
                />
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
