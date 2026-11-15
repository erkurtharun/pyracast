import type { Metadata } from "next";
import { getLocale } from "next-intl/server";
import "./globals.css";
export const metadata: Metadata = {
    metadataBase: new URL("https://pyracast.app"),
    title: { default: "Pyracast", template: "%s | Pyracast" },
    applicationName: "Pyracast",
};
export default async function Layout({
    children,
}: Readonly<{ children: React.ReactNode }>) {
    const locale = await getLocale();
    return (
        <html lang={locale}>
            <body>{children}</body>
        </html>
    );
}
