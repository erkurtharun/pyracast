import { LegalPage } from "@/features/public-site/legal-page";
import { legalCopy } from "@/features/public-site/legal-copy";
import { publicMetadata } from "@/lib/public-metadata";

export const metadata = publicMetadata("en", "terms");

export default function Page() {
    return <LegalPage language="en" {...legalCopy("en", "terms")} />;
}
