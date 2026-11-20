"use client";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api/client";
import { useApiQuery, useRefresh } from "@/lib/api/query";
import { useFormat } from "@/lib/format/use-format";
import type { components } from "@/lib/schema";
import { AsyncButton } from "@/ui/form";
import { Resource } from "@/ui/resource";
import { StatusTag, useLabel } from "@/ui/vocabulary";

type Invitation = components["schemas"]["InvitationOutput"];

export function PendingInvitations({ tenantId }: { tenantId: number }) {
    const t = useTranslations("WorkspaceMembers");
    const format = useFormat();
    const label = useLabel();
    const refresh = useRefresh();
    const invitations = useApiQuery("/api/tenants/{tenant_id}/invitations/", {
        path: { tenant_id: tenantId },
    });
    const path = (invitation: Invitation) => ({
        path: { tenant_id: tenantId, invitation_id: invitation.id },
    });
    async function act(send: () => Promise<unknown>) {
        try {
            await send();
        } finally {
            await refresh.workspace(tenantId);
        }
    }
    return (
        <section className="panel narrow">
            <h2>{t("pending")}</h2>
            <Resource resource={invitations} loading={t("loading")}>
                {({ invitations: rows }) =>
                    rows.length ? (
                        <ul className="member-list">
                            {rows.map((invitation) => (
                                <li
                                    key={invitation.id}
                                    className="workspace-member"
                                >
                                    <span>
                                        <span>{invitation.email}</span>
                                        <StatusTag
                                            group="InvitationState"
                                            value={invitation.state}
                                        />
                                        <small className="field-hint">
                                            {label(
                                                "WorkspaceRole",
                                                invitation.role,
                                            )}{" "}
                                            ·{" "}
                                            {t("sentOn", {
                                                date: format.deviceTime(
                                                    invitation.created_at,
                                                ),
                                            })}{" "}
                                            ·{" "}
                                            {t(
                                                invitation.state === "pending"
                                                    ? "expiresOn"
                                                    : "expiredOn",
                                                {
                                                    date: format.deviceTime(
                                                        invitation.expires_at,
                                                    ),
                                                },
                                            )}
                                        </small>
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
                                    <div className="button-group">
                                        <AsyncButton
                                            variant="secondary"
                                            label={t("resendNamed", {
                                                email: invitation.email,
                                            })}
                                            errorTitle={t("resendFailed")}
                                            success={t("resent", {
                                                email: invitation.email,
                                            })}
                                            onClick={() =>
                                                act(() =>
                                                    api.post(
                                                        "/api/tenants/{tenant_id}/invitations/{invitation_id}/resend/",
                                                        path(invitation),
                                                    ),
                                                )
                                            }
                                        >
                                            {t("resend")}
                                        </AsyncButton>
                                        <AsyncButton
                                            variant="secondary"
                                            label={t("cancelNamed", {
                                                email: invitation.email,
                                            })}
                                            errorTitle={t("cancelFailed")}
                                            success={t("cancelled")}
                                            onClick={() =>
                                                act(() =>
                                                    api.delete(
                                                        "/api/tenants/{tenant_id}/invitations/{invitation_id}/",
                                                        path(invitation),
                                                    ),
                                                )
                                            }
                                        >
                                            {t("cancel")}
                                        </AsyncButton>
                                    </div>
                                </li>
                            ))}
                        </ul>
                    ) : (
                        <p>{t("noInvitations")}</p>
                    )
                }
            </Resource>
        </section>
    );
}
