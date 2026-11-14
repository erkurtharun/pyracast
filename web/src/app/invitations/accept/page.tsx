import { Suspense } from "react";
import { Dashboard } from "@/components/dashboard";

export default function InvitationPage() {
    return (
        <Suspense>
            <Dashboard />
        </Suspense>
    );
}
