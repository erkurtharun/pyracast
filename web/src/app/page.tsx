import { Dashboard } from "@/components/dashboard";
import { connection } from "next/server";

export default async function Page() {
    await connection();
    return <Dashboard />;
}
