import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { connection } from "next/server";
import { Suspense } from "react";
import { AppRoot } from "@/components/app-root";

export const metadata: Metadata = {
    title: "Pyracast workspace",
    robots: { index: false, follow: false },
};

export default async function AppLayout({
    children,
}: Readonly<{ children: React.ReactNode }>) {
    await connection();
    return (
        <NextIntlClientProvider>
            <Suspense>
                <AppRoot>{children}</AppRoot>
            </Suspense>
        </NextIntlClientProvider>
    );
}
