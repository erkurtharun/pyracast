import { notFound } from "next/navigation";
import type { ReactNode } from "react";
import { PlantSite } from "@/features/plant/plant-site";
import { routeId } from "@/lib/routes";

export default async function PlantLayout({
    children,
    params,
}: {
    children: ReactNode;
    params: Promise<{ siteId: string }>;
}) {
    const siteId = routeId((await params).siteId);
    if (!siteId) notFound();
    return <PlantSite siteId={siteId}>{children}</PlantSite>;
}
