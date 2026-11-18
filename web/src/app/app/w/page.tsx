import { RequireUser } from "@/components/require-user";
import { WorkspaceIndex } from "@/components/workspace-pages";
import { WorkspaceShell } from "@/components/workspace-shell";

export default function WorkspacesPage() {
    return (
        <RequireUser>
            <WorkspaceShell>
                <WorkspaceIndex />
            </WorkspaceShell>
        </RequireUser>
    );
}
