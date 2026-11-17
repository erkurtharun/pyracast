import type { Metadata } from "next";
import { languages, type Language } from "@/i18n/language";

export const siteOrigin = "https://pyracast.app";
export const publicPages = ["home", "privacy", "terms"] as const;
export type PublicPage = (typeof publicPages)[number];

export function publicPath(language: Language, page: PublicPage): string {
    const prefix = language === "tr" ? "/tr" : "";
    return `${prefix}${page === "home" ? "" : `/${page}`}/`;
}

export function publicRoute(
    pathname: string,
): { language: Language; page: PublicPage } | undefined {
    for (const language of languages)
        for (const page of publicPages) {
            const path = publicPath(language, page);
            if (pathname === path || (path !== "/" && `${pathname}/` === path))
                return { language, page };
        }
    return undefined;
}

const descriptions = {
    en: {
        home: {
            title: "Solar production forecasts",
            description:
                "Plan your solar operations with production forecasts, reported generation, outage scenarios and scheduled email reports in one workspace.",
        },
        privacy: {
            title: "Privacy policy",
            description:
                "How Pyracast handles account, plant and sign-in data, uses cookies and service providers, and responds to privacy requests.",
        },
        terms: {
            title: "Terms of use",
            description:
                "Pyracast terms: responsible use, workspace access, forecast limitations and account security.",
        },
    },
    tr: {
        home: {
            title: "Güneş enerjisi üretim tahmini",
            description:
                "Güneş santrallerinizin üretim tahminlerini, gerçekleşen üretimini, kesinti senaryolarını ve e-posta raporlarını tek çalışma alanında takip edin.",
        },
        privacy: {
            title: "Gizlilik politikası",
            description:
                "Pyracast hesap, santral ve giriş verilerini nasıl işler? Çerezler, hizmet sağlayıcıları ve gizlilik başvuruları hakkında bilgi alın.",
        },
        terms: {
            title: "Kullanım koşulları",
            description:
                "Pyracast kullanım koşulları: sorumlu kullanım, çalışma alanı erişimi, tahminlerin sınırları ve hesap güvenliği.",
        },
    },
};

export function publicMetadata(language: Language, page: PublicPage): Metadata {
    const { title, description } = descriptions[language][page];
    const canonical = publicPath(language, page);
    const image =
        language === "tr" ? "/tr/opengraph-image" : "/opengraph-image";
    const imageAlt =
        language === "tr"
            ? "Pyracast — güneş enerjisi üretim tahmini"
            : "Pyracast — solar production forecasts";
    return {
        title: { absolute: `${title} | Pyracast` },
        description,
        alternates: {
            canonical,
            languages: {
                en: publicPath("en", page),
                tr: publicPath("tr", page),
                "x-default": publicPath("en", page),
            },
        },
        robots: { index: true, follow: true },
        openGraph: {
            title: `${title} | Pyracast`,
            description,
            siteName: "Pyracast",
            url: canonical,
            type: "website",
            locale: language === "tr" ? "tr_TR" : "en_US",
            alternateLocale: language === "tr" ? "en_US" : "tr_TR",
            images: [{ url: image, width: 1200, height: 630, alt: imageAlt }],
        },
        twitter: {
            card: "summary_large_image",
            title: `${title} | Pyracast`,
            description,
            images: [{ url: image, alt: imageAlt }],
        },
    };
}
