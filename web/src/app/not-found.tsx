import type { Metadata } from "next";
import { NotFoundPage } from "@/features/public-site/not-found-page";

export const metadata: Metadata = {
    title: { absolute: "Page not found | Pyracast" },
};

// The root locale here is the cookie or English, never the requested path, so both languages show.
export default function NotFound() {
    return <NotFoundPage />;
}
