import type { MetadataRoute } from "next";
import { siteOrigin } from "@/lib/public-metadata";

export default function robots(): MetadataRoute.Robots {
    return {
        rules: {
            userAgent: "*",
            allow: "/",
            disallow: ["/api/", "/admin/", "/health/"],
        },
        sitemap: `${siteOrigin}/sitemap.xml`,
    };
}
