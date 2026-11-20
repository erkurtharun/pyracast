"use client";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api/client";
import { useApiQuery, useRefresh } from "@/lib/api/query";
import { ActionForm, Notice } from "@/ui/ui";

export function PendingInvitations({ tenantId }: { tenantId: number }) {
    const t = useTranslations("WorkspaceMembers");
    const refresh = useRefresh();
    const invitations = useApiQuery("/api/tenants/{tenant_id}/invitations/", {
        path: { tenant_id: tenantId },
    });
    return (
        <section className="panel narrow">
            <h2>{t("pending")}</h2>
            {invitations.loading && <p role="status">{t("loading")}</p>}
            {invitations.error && <Notice error>{invitations.error}</Notice>}
            {invitations.data &&
                (invitations.data.invitations.length ? (
                    <ul className="member-list">
                        {invitations.data.invitations.map((invitation) => (
                            <li key={invitation.id}>
                                <span>
                                    {invitation.email}
                                    {invitation.delivery_status ===
                                        "uncertain" && (
                                        <small className="field-hint">
                                            {t("deliveryUncertain")}
                                        </small>
                                    )}
                                    {invitation.delivery_status ===
                                        "failed" && (
                                        <small className="field-hint">
                                            {t("deliveryFailed")}
                                        </small>
                                    )}
                                </span>
                                <ActionForm
                                    children={null}
                                    submit={t("cancel")}
                                    onSubmit={async () => {
                                        await api.delete(
                                            "/api/tenants/{tenant_id}/invitations/{invitation_id}/",
                                            {
                                                path: {
                                                    tenant_id: tenantId,
                                                    invitation_id:
                                                        invitation.id,
                                                },
                                            },
                                        );
                                        await refresh.workspace(tenantId);
                                    }}
                                />
                            </li>
                        ))}
                    </ul>
                ) : (
                    <p>{t("noInvitations")}</p>
                ))}
        </section>
    );
}
