import type { Metadata, Viewport } from "next";
import { NextIntlClientProvider } from "next-intl";
import { connection } from "next/server";
import { Suspense } from "react";
import { AppRoot } from "@/features/session/app-root";
import { appWindowColour } from "@/styles/tokens";

// Next streams page metadata into a new boundary on every client navigation, which leaves the document untitled until it arrives; the layout keeps one title mounted instead.
export const metadata: Metadata = {
    title: null,
    robots: { index: false, follow: false },
    manifest: "/app/manifest.webmanifest",
    icons: {
        icon: { url: "/icon.svg", type: "image/svg+xml", sizes: "any" },
        apple: "/app/apple-touch-icon.png",
    },
};

export const viewport: Viewport = { themeColor: appWindowColour };

export default async function AppLayout({
    children,
}: Readonly<{ children: React.ReactNode }>) {
    await connection();
    return (
        <NextIntlClientProvider messages={null}>
            <title>Pyracast workspace | Pyracast</title>
            <Suspense>
                <AppRoot>{children}</AppRoot>
            </Suspense>
        </NextIntlClientProvider>
    );
}
