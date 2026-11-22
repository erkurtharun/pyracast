"use client";
import { recovery } from "@/lib/on-demand";
import { RouteError } from "@/ui/route-error";

export default function PlantError({
    error,
    retry,
}: {
    error: unknown;
    retry: () => void;
}) {
    return <RouteError retry={recovery(error, retry)} />;
}
