import { HomePage } from "@/components/public-site/home-page";
import { publicMetadata } from "@/lib/public-metadata";
import { redirect } from "next/navigation";
import { WebsiteData } from "@/components/public-site/website-data";

export const metadata = publicMetadata("en", "home");

export default async function Page({
    searchParams,
}: {
    searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
    const params = await searchParams;
    if (
        ["social_callback", "account", "workspace", "token", "next"].some(
            (key) => key in params,
        )
    ) {
        const query = new URLSearchParams();
        for (const [key, value] of Object.entries(params)) {
            if (typeof value === "string") query.append(key, value);
            else if (Array.isArray(value))
                for (const item of value) query.append(key, item);
        }
        redirect(`/app/?${query}`);
    }
    return (
        <>
            <WebsiteData />
            <HomePage language="en" />
        </>
    );
}
