import { NextIntlClientProvider } from "next-intl";

export default function AppLayout({
    children,
}: Readonly<{ children: React.ReactNode }>) {
    return <NextIntlClientProvider>{children}</NextIntlClientProvider>;
}
