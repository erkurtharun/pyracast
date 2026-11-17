import type { MetadataRoute } from "next";
import { languages } from "@/i18n/language";
import { publicPages, publicPath, siteOrigin } from "@/lib/public-metadata";

export default function sitemap(): MetadataRoute.Sitemap {
    return publicPages.flatMap((page) =>
        languages.map((language) => ({
            url: `${siteOrigin}${publicPath(language, page)}`,
            alternates: {
                languages: {
                    en: `${siteOrigin}${publicPath("en", page)}`,
                    tr: `${siteOrigin}${publicPath("tr", page)}`,
                    "x-default": `${siteOrigin}${publicPath("en", page)}`,
                },
            },
        })),
    );
}
