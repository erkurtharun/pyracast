import { RequireUser } from "@/features/session/require-user";
import { WorkspaceIndex } from "@/features/workspace/workspace-pages";
import { WorkspaceShell } from "@/features/workspace/workspace-shell";

export default function WorkspacesPage() {
    return (
        <RequireUser>
            <WorkspaceShell>
                <WorkspaceIndex />
            </WorkspaceShell>
        </RequireUser>
    );
}
