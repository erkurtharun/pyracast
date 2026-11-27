"use client";
import { useTranslations } from "next-intl";
import { useKeptFiguresPreference } from "@/features/saved/saved-figures";
import { CheckboxField, FieldErrors } from "@/ui/form";

export function KeptFiguresSetting() {
    const t = useTranslations("KeptFigures");
    const common = useTranslations("Common");
    const preference = useKeptFiguresPreference();
    if (!preference) return null;
    return (
        <section
            className="panel narrow"
            aria-busy={preference.saving || undefined}
        >
            <FieldErrors
                value={preference.failed ? { kept: common("saveFailed") } : {}}
            >
                <CheckboxField
                    name="kept"
                    label={t("label")}
                    hint={t("hint")}
                    checked={preference.kept}
                    onChange={preference.change}
                />
            </FieldErrors>
            {preference.saving && (
                <p className="field-hint" role="status">
                    {common("saving")}
                </p>
            )}
        </section>
    );
}
