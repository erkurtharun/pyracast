"use client";
import { RouteError } from "@/ui/route-error";

export default function AppError({
    retry,
}: {
    error: unknown;
    retry: () => void;
}) {
    return (
        <main className="auth-page">
            <RouteError retry={retry} startLink />
        </main>
    );
}
