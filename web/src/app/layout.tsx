import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
    title: "Pyracast · Solar intelligence",
    description: "Your solar forecasts, plants and production in one place.",
};
export default function Layout({
    children,
}: Readonly<{ children: React.ReactNode }>) {
    return (
        <html lang="en">
            <body>{children}</body>
        </html>
    );
}
