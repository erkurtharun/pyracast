import type { Metadata } from "next";
import { connection } from "next/server";
import { Dashboard } from "@/components/dashboard";

export const metadata: Metadata = {
    title: "Pyracast workspace",
    robots: { index: false, follow: false },
};

export default async function AppPage() {
    await connection();
    return <Dashboard />;
}
