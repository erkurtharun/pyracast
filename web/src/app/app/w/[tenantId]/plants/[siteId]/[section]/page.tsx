import { notFound } from "next/navigation";
import { isPlantSection } from "@/lib/routes";

export default async function PlantSectionPage({
    params,
}: {
    params: Promise<{ section: string }>;
}) {
    if (!isPlantSection((await params).section)) notFound();
    return null;
}
