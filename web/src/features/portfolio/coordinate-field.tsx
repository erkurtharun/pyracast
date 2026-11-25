"use client";
import { useTranslations } from "next-intl";
import type { ClipboardEvent } from "react";
import { Field } from "@/ui/form";
import {
    coordinatePair,
    coordinatePattern,
    type CoordinateAxis,
} from "./site-location";

// React drops an input event whose value was assigned through `value`, so the text is entered as an edit.
function enter(field: HTMLInputElement, text: string) {
    field.setRangeText(text, 0, field.value.length, "end");
    field.dispatchEvent(new Event("input", { bubbles: true }));
}

// A pair pasted into Latitude fills the form's Longitude too; any other text is pasted as is.
function fillPair(event: ClipboardEvent<HTMLInputElement>) {
    const pair = coordinatePair(event.clipboardData.getData("text"));
    const longitude = event.currentTarget.form?.elements.namedItem("longitude");
    if (!pair || !(longitude instanceof HTMLInputElement)) return;
    event.preventDefault();
    enter(event.currentTarget, pair[0]);
    enter(longitude, pair[1]);
}

// A text field: a number field drops or reinterprets a decimal comma depending on the browser and
// the page language, and hides the text it refused from scripts.
export function CoordinateField({
    axis,
    label,
    defaultValue,
}: {
    axis: CoordinateAxis;
    label: string;
    defaultValue?: number;
}) {
    const t = useTranslations("CoordinateField");
    const latitude = axis === "latitude";
    return (
        <Field
            label={label}
            name={axis}
            pattern={coordinatePattern(axis)}
            title={t(axis)}
            defaultValue={defaultValue}
            hint={latitude ? t("pasteHint") : undefined}
            onPaste={latitude ? fillPair : undefined}
            required
        />
    );
}
