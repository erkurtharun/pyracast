import { LegalPage } from "@/components/public-site/legal-page";
import { legalCopy } from "@/components/public-site/legal-copy";
import { publicMetadata } from "@/lib/public-metadata";

export const metadata = publicMetadata("tr", "privacy");

export default function Page() {
    return (
        <LegalPage
            language="tr"

            {...legalCopy("tr", "privacy")}
        />
    );
}
