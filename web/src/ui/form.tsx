import { useId, type ReactNode } from "react";

export function ControlField(
    props:
        | { label: ReactNode; hint?: undefined; children: ReactNode }
        | {
              label: ReactNode;
              hint: string;
              children: (describedBy: string) => ReactNode;
          },
) {
    const hintId = `${useId()}-hint`;
    if (props.hint === undefined)
        return (
            <label className="field">
                {props.label}
                {props.children}
            </label>
        );
    return (
        <div className="field">
            <label className="field-control">
                {props.label}
                {props.children(hintId)}
            </label>
            <span className="field-hint" id={hintId}>
                {props.hint}
            </span>
        </div>
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
