import type { Metadata } from "next";
import { getLocale } from "next-intl/server";
import type { ReactNode } from "react";
import "@/styles/tokens.css";
import "./globals.css";

const appName = "Pyracast";

// A browser installs a page without a manifest link as an app of its own, so every page links the one application.
// Declaring `icons` drops the favicon of `icon.svg`, so it is named here.
export const metadata: Metadata = {
    metadataBase: new URL("https://pyracast.app"),
    title: { default: appName, template: `%s | ${appName}` },
    applicationName: appName,
    appleWebApp: { title: appName, capable: false },
    manifest: "/app/manifest.webmanifest",
    icons: {
        icon: { url: "/icon.svg", type: "image/svg+xml", sizes: "any" },
        apple: "/app/icons/apple-touch-icon.png",
    },
};
export default async function Layout({
    children,
}: Readonly<{ children: ReactNode }>) {
    const locale = await getLocale();
    return (
        <html lang={locale}>
            <body>{children}</body>
        </html>
    );
}
