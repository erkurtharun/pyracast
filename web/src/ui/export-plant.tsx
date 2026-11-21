"use client";
import { createContext, useContext } from "react";
import type { PlantRef } from "@/lib/exports";

// The plant that every export on the plant screens names; the plant workspace provides it.
export const ExportPlant = createContext<PlantRef | null>(null);

export function useExportPlant(): PlantRef {
    const plant = useContext(ExportPlant);
    if (!plant) throw new Error("A plant export needs its plant.");
    return plant;
}
