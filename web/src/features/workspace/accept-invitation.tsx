"use client";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api/client";
import { routes } from "@/lib/routes";
import { signOut, useSessionState } from "@/lib/session-client";
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
                        onClick={async () => {
                            await api.post("/api/invitations/accept/", {
                                body: { token: invitationToken },
                            });
                            clearWorkspaceInvitation();
                            window.location.replace(routes.workspaces());
                        }}
                    >
                        {t("accept")}
                    </AsyncButton>
                )}
                <div>
                    <button
                        type="button"
                        className="button secondary"
                        onClick={() =>
                            void signOut({ keepInvitation: true }).catch(
                                () => {},
                            )
                        }
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
