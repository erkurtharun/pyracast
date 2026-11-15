import { HomePage } from "@/components/public-site/home-page";
import { publicMetadata } from "@/lib/public-metadata";
import { WebsiteData } from "@/components/public-site/website-data";

export const metadata = publicMetadata("tr", "home");

export default function Page() {
    return (
        <>
            <WebsiteData />
            <HomePage language="tr" />
        </>
    );
}
