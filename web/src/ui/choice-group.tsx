"use client";
import { useId } from "react";

export function ChoiceGroup<K extends string>({
    label,
    choices,
    value,
    onChange,
}: {
    label: string;
    choices: readonly (readonly [K, string])[];
    value: K;
    onChange: (choice: K) => void;
}) {
    const name = useId();
    return (
        <fieldset className="choice-group">
            <legend>{label}</legend>
            <div>
                {choices.map(([key, text]) => (
                    <label key={key}>
                        <input
                            type="radio"
                            name={name}
                            value={key}
                            checked={value === key}
                            onChange={() => onChange(key)}
                        />
                        {text}
                    </label>
                ))}
            </div>
        </fieldset>
    );
}
