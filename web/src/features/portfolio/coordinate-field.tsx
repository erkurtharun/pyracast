"use client";
import { useTranslations } from "next-intl";
import { Field } from "@/ui/form";
import { coordinatePattern, type CoordinateAxis } from "./site-location";

// A text field: a number field drops or reinterprets a decimal comma depending on the browser and
// the page language, and hides the text it refused from scripts.
export function CoordinateField({
    axis,
    label,
    defaultValue,
    hint,
}: {
    axis: CoordinateAxis;
    label: string;
    defaultValue?: number;
    hint?: string;
}) {
    const t = useTranslations("CoordinateField");
    return (
        <Field
            label={label}
            name={axis}
            inputMode="decimal"
            pattern={coordinatePattern(axis)}
            title={t(axis)}
            defaultValue={defaultValue}
            hint={hint}
            required
        />
    );
}
