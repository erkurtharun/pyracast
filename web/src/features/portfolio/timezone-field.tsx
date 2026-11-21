"use client";
import { useState } from "react";
import { Field, Select } from "@/ui/form";
import { supportedZones, zoneChoices } from "./site-location";

// Without the browser's zone list the zone is typed; the server checks it either way.
export function TimezoneField({
    label,
    defaultValue,
}: {
    label: string;
    defaultValue: string;
}) {
    const [zones] = useState(() => {
        const supported = supportedZones();
        return supported && zoneChoices(supported, defaultValue);
    });
    return zones ? (
        <Select
            label={label}
            name="timezone"
            defaultValue={defaultValue}
            required
        >
            {zones.map((zone) => (
                <option key={zone} value={zone}>
                    {zone}
                </option>
            ))}
        </Select>
    ) : (
        <Field
            label={label}
            name="timezone"
            maxLength={100}
            defaultValue={defaultValue}
            placeholder="Europe/Istanbul"
            required
        />
    );
}
