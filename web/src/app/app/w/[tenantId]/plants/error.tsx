"use client";
import { RouteError } from "@/components/route-error";

export default function PlantError({
    retry,
}: {
    error: unknown;
    retry: () => void;
}) {
    return <RouteError retry={retry} />;
}
