import { LegalPage } from "@/features/public-site/legal-page";
import { legalCopy } from "@/features/public-site/legal-copy";
import { publicMetadata } from "@/lib/public-metadata";

export const metadata = publicMetadata("tr", "terms");

export default function Page() {
    return <LegalPage language="tr" {...legalCopy("tr", "terms")} />;
}
