import { headers } from "next/headers";
import { siteOrigin } from "@/lib/public-metadata";

export async function WebsiteData() {
    const nonce = (await headers()).get("x-nonce") ?? undefined;
    return (
        <script type="application/ld+json" nonce={nonce}>
            {JSON.stringify({
                "@context": "https://schema.org",
                "@type": "WebSite",
                name: "Pyracast",
                url: `${siteOrigin}/`,
                inLanguage: ["en", "tr"],
            })}
        </script>
    );
}
