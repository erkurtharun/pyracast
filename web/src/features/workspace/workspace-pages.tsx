"use client";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { routes } from "@/lib/routes";
import { OperationalInbox } from "@/features/inbox/operational-inbox";
import { RecycleBin } from "@/features/trash/recycle-bin";
import { WorkspaceMembers } from "./workspace-members";
import {
    useNavigating,
    useOptionalWorkspace,
    useWorkspace,
} from "./workspace-shell";

// A later replace discards a pending push, so a redirect yields to the shell navigation the
// person started; the transition stays pending until the router commits or drops that push.
function Replace({ href }: { href: string }) {
    const router = useRouter();
    const navigating = useNavigating();
    useEffect(() => {
        if (!navigating) router.replace(href);
    }, [navigating, router, href]);
    return null;
}

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
    return (
        <OperationalInbox key={`${tenant.id}:${tenant.role}`} tenant={tenant} />
    );
}

export function TrashPage() {
    const t = useTranslations("Dashboard");
    const router = useRouter();
    const { tenant } = useWorkspace();
    return (
        <AdminOnly>
            <button
                className="back-button"
                onClick={() => router.push(routes.portfolio(tenant.id))}
            >
                {t("overview")}
            </button>
            <RecycleBin key={tenant.id} tenant={tenant.id} />
        </AdminOnly>
    );
}
