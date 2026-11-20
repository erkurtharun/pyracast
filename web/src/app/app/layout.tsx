import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { connection } from "next/server";
import { Suspense } from "react";
import { AppRoot } from "@/features/session/app-root";

// Next streams page metadata into a new boundary on every client navigation, which leaves the document untitled until it arrives; the layout keeps one title mounted instead.
export const metadata: Metadata = {
    title: null,
    robots: { index: false, follow: false },
};

export default async function AppLayout({
    children,
}: Readonly<{ children: React.ReactNode }>) {
    await connection();
    return (
        <NextIntlClientProvider>
            <title>Pyracast workspace | Pyracast</title>
            <Suspense>
                <AppRoot>{children}</AppRoot>
            </Suspense>
        </NextIntlClientProvider>
    );
}
