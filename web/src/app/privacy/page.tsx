import { LegalPage } from "@/components/public-site/legal-page";
import { legalCopy } from "@/components/public-site/legal-copy";
import { publicMetadata } from "@/lib/public-metadata";

export const metadata = publicMetadata("en", "privacy");

export default function Page() {
    return (
        <LegalPage
            language="en"

            {...legalCopy("en", "privacy")}
        />
    );
}
