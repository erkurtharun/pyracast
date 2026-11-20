import { HomePage } from "@/features/public-site/home-page";
import { publicMetadata } from "@/lib/public-metadata";
import { WebsiteData } from "@/features/public-site/website-data";

export const metadata = publicMetadata("tr", "home");

export default function Page() {
    return (
        <>
            <WebsiteData />
            <HomePage language="tr" />
        </>
    );
}
