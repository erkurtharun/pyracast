import { AccountSettings } from "@/features/account/account-settings";
import { RequireUser } from "@/features/session/require-user";
import { WorkspaceShell } from "@/features/workspace/workspace-shell";

export default function AccountPage() {
    return (
        <RequireUser>
            <WorkspaceShell>
                <AccountSettings />
            </WorkspaceShell>
        </RequireUser>
    );
}
