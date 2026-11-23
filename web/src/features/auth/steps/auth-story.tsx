"use client";
import { useTranslations } from "next-intl";
import { Brand } from "@/ui/brand";

export function AuthStory() {
    const t = useTranslations("Auth");
    return (
        <section className="auth-story" aria-label="Pyracast">
            <Brand surface="dark" />
            <div>
                <p className="eyebrow">{t("solarIntelligence")}</p>
                <h2>
                    {t("portfolio")}
                    <br />

                    {t("oneView")}
                </h2>
                <p>{t("intro")}</p>
            </div>
            <div className="auth-story-footer">{t("builtForEnergy")}</div>
        </section>
    );
}
