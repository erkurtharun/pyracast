import type { Metadata } from "next";
import { getLocale } from "next-intl/server";
import type { ReactNode } from "react";
import "@/styles/tokens.css";
import "./globals.css";
export const metadata: Metadata = {
    metadataBase: new URL("https://pyracast.app"),
    title: { default: "Pyracast", template: "%s | Pyracast" },
    applicationName: "Pyracast",
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
