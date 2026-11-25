import { ImageResponse } from "next/og";
import type { Language } from "@/i18n/language";
import {
    socialImageColours as colours,
    socialImageSpacing as spacing,
} from "@/styles/tokens";
import { Brand } from "@/ui/brand";

export function createSocialImage(language: Language) {
    return new ImageResponse(
        <div
            style={{
                display: "flex",
                flexDirection: "column",
                justifyContent: "space-between",
                width: "100%",
                height: "100%",
                padding: spacing.page,
                background: colours.background,
                color: colours.text,
            }}
        >
            <Brand surface="dark" height={64} />
            <div
                style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: spacing.lines,
                }}
            >
                <div style={{ fontSize: 76, maxWidth: 900, lineHeight: 1.1 }}>
                    {language === "tr"
                        ? "Yarının üretimini bugünden planlayın."
                        : "Plan tomorrow’s solar production."}
                </div>
                <div style={{ color: colours.muted, fontSize: 28 }}>
                    {language === "tr"
                        ? "Üretim tahmini · Gerçekleşen üretim · Tek çalışma alanı"
                        : "Generation forecast · Actual production · One workspace"}
                </div>
            </div>
            <div style={{ color: colours.accent, fontSize: 24 }}>
                pyracast.app
            </div>
        </div>,
        { width: 1200, height: 630 },
    );
}
