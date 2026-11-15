import { Suspense } from "react";
import { Dashboard } from "@/components/dashboard";
import type { Metadata } from "next";

export const metadata: Metadata = {
    robots: { index: false, follow: false },
};

export default function InvitationPage() {
    return (
        <Suspense>
            <Dashboard />
        </Suspense>
    );
}
