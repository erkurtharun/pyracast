import { notFound } from "next/navigation";
import { RequireUser } from "@/features/session/require-user";
import { WorkspaceShell } from "@/features/workspace/workspace-shell";
import { routeId } from "@/lib/routes";

export default async function WorkspaceLayout({
    children,
    params,
}: {
    children: React.ReactNode;
    params: Promise<{ tenantId: string }>;
}) {
    const tenantId = routeId((await params).tenantId);
    if (!tenantId) notFound();
    return (
        <RequireUser>
            <WorkspaceShell tenantId={tenantId}>{children}</WorkspaceShell>
        </RequireUser>
    );
}
