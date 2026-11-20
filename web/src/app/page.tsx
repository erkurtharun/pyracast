import { HomePage } from "@/features/public-site/home-page";
import { publicMetadata } from "@/lib/public-metadata";
import { WebsiteData } from "@/features/public-site/website-data";

export const metadata = publicMetadata("en", "home");

export default function Page() {
    return (
        <>
            <WebsiteData />
            <HomePage language="en" />
        </>
    );
}
