import type { Metadata } from "next";
import { getLocale } from "next-intl/server";
import type { ReactNode } from "react";
import "@/styles/tokens.css";
import "./globals.css";

const appName = "Pyracast";

// Every page, not only those under /app/, links the application's manifest, touch icon and name.
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
