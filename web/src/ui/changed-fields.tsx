"use client";
import { useTranslations } from "next-intl";

export type FieldChange = {
    field: string;
    label: string;
    before: string;
    after: string;
    note?: string;
};

export function ChangedFields({ changes }: { changes: FieldChange[] }) {
    const t = useTranslations("Common");
    return (
        <ul>
            {changes.map((change) => (
                <li key={change.field}>
                    {t("changedField", {
                        field: change.label,
                        before: change.before,
                        after: change.after,
                    })}
                    {change.note && ` ${change.note}`}
                </li>
            ))}
        </ul>
    );
}
