"use client";
import { useTranslations } from "next-intl";
import { useKeptFiguresPreference } from "@/features/saved/saved-figures";
import { CheckboxField } from "@/ui/form";

export function KeptFiguresSetting() {
    const t = useTranslations("KeptFigures");
    const preference = useKeptFiguresPreference();
    if (!preference) return null;
    return (
        <section className="panel narrow">
            <CheckboxField
                label={t("label")}
                hint={t("hint")}
                checked={preference.kept}
                onChange={preference.change}
            />
        </section>
    );
}
