"use client";

import { usePathname } from "next/navigation";

type Target = { href: string; label: string };

export function NotFoundLink({ home, app }: { home: Target; app: Target }) {
    const target = usePathname().startsWith(app.href) ? app : home;
    return <a href={target.href}>{target.label}</a>;
}
