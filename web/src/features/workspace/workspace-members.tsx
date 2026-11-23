"use client";
import { useTranslations } from "next-intl";
import type { Tenant } from "@/lib/api";
import { usePageTitle } from "@/ui/page-title";
import { InviteMember } from "./members/invite-member";
import { MemberList } from "./members/member-list";
import { PendingInvitations } from "./members/pending-invitations";

export function WorkspaceMembers({
    tenant,
    userId,
}: {
    tenant: Tenant;
    userId: number;
}) {
    const t = useTranslations("WorkspaceMembers");
    usePageTitle(`${t("title")} · ${tenant.name}`);
    return (
        <>
            <div className="page-heading">
                <div>
                    <p className="eyebrow name">{tenant.name}</p>
                    <h1>{t("title")}</h1>
                </div>
            </div>
            <InviteMember tenantId={tenant.id} />
            <MemberList tenant={tenant} userId={userId} />
            <PendingInvitations tenantId={tenant.id} />
        </>
    );
}
