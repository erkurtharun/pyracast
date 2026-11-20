"use client";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api/client";
import { useApiQuery, useRefresh } from "@/lib/api/query";
import { ActionForm, Field, Notice } from "@/ui/ui";

export function InviteMember({ tenantId }: { tenantId: number }) {
    const t = useTranslations("WorkspaceMembers");
    const refresh = useRefresh();
    const members = useApiQuery("/api/tenants/{tenant_id}/members/", {
        path: { tenant_id: tenantId },
    });
    return (
        <section className="panel narrow">
            <h2>{t("invite")}</h2>
            {!!members.data?.plants_at_recipient_limit && (
                <Notice>
                    {t("reminderLimitWarning", {
                        count: members.data.plants_at_recipient_limit,
                    })}
                </Notice>
            )}
            <ActionForm
                submit={t("sendInvitation")}
                onSubmit={async (data) => {
                    await api.post("/api/tenants/{tenant_id}/invitations/", {
                        path: { tenant_id: tenantId },
                        body: { email: String(data.get("email") ?? "") },
                    });
                    await refresh.workspace(tenantId);
                }}
            >
                <Field
                    label={t("email")}
                    name="email"
                    type="email"
                    autoComplete="email"
                    maxLength={254}
                />
            </ActionForm>
        </section>
    );
}
