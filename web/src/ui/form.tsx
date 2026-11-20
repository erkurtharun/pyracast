import type { ReactNode } from "react";

export function ControlField({
    label,
    children,
}: {
    label: ReactNode;
    children: ReactNode;
}) {
    return (
        <label className="field">
            {label}
            {children}
        </label>
    );
}

export function CheckboxField({
    label,
    checked,
    disabled,
    onChange,
}: {
    label: string;
    checked: boolean;
    disabled?: boolean;
    onChange: (checked: boolean) => void;
}) {
    return (
        <label className="field">
            <span>
                <input
                    type="checkbox"
                    checked={checked}
                    disabled={disabled}
                    onChange={(event) => onChange(event.target.checked)}
                />{" "}
                {label}
            </span>
        </label>
    );
}
