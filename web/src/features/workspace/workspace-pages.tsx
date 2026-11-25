"use client";
import { useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import type { ReactNode } from "react";
import { useNavigate } from "@/lib/leave-guard";
import { routeId, routes } from "@/lib/routes";
import { OperationalInbox } from "@/features/inbox/operational-inbox";
import { RecycleBin } from "@/features/trash/recycle-bin";
import { Replace } from "./redirect";
import { WorkspaceMembers } from "./workspace-members";
import { useOptionalWorkspace, useWorkspace } from "./workspace-shell";

function AdminOnly({ children }: { children: ReactNode }) {
    const { tenant } = useWorkspace();
    return tenant.role === "admin" ? (
        children
    ) : (
        <Replace href={routes.portfolio(tenant.id)} />
    );
}

export function WorkspaceIndex() {
    const tenant = useOptionalWorkspace();
    return tenant ? <Replace href={routes.portfolio(tenant.id)} /> : null;
}

export function MembersPage() {
    const { tenant, user } = useWorkspace();
    return (
        <AdminOnly>
            <WorkspaceMembers
                key={tenant.id}
                tenant={tenant}
                userId={user.id}
            />
        </AdminOnly>
    );
}

export function InboxPage() {
    const { tenant } = useWorkspace();
    const linked = routeId(useSearchParams().get("request") ?? undefined);
    return (
        <OperationalInbox
            key={`${tenant.id}:${tenant.role}`}
            tenant={tenant}
            linked={linked}
        />
    );
}

export function TrashPage() {
    const t = useTranslations("Dashboard");
    const navigate = useNavigate();
    const { tenant } = useWorkspace();
    return (
        <AdminOnly>
            <button
                className="back-button"
                onClick={() => navigate(routes.portfolio(tenant.id))}
            >
                {t("overview")}
            </button>
            <RecycleBin key={tenant.id} tenant={tenant.id} />
        </AdminOnly>
    );
}
