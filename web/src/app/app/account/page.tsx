import { AccountSettings } from "@/components/account-settings";
import { RequireUser } from "@/components/require-user";
import { WorkspaceShell } from "@/components/workspace-shell";

export default function AccountPage() {
    return (
        <RequireUser>
            <WorkspaceShell>
                <AccountSettings />
            </WorkspaceShell>
        </RequireUser>
    );
}
