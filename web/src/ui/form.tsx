"use client";
import { useTranslations } from "next-intl";
import {
    createContext,
    useContext,
    useEffect,
    useId,
    useRef,
    useState,
    type ChangeEventHandler,
    type InputHTMLAttributes,
    type ReactNode,
    type RefObject,
} from "react";
import { ApiError, displayMessage } from "@/lib/api/errors";
import { useAnnounce } from "./live-region";
import { useCanManageWorkspace } from "./workspace-access";

type Variant = "primary" | "secondary" | "danger";
type FieldEntry = { name: string; message: string; label: string | null };

const FormErrors = createContext<Record<string, string>>({});

const buttonClass = (variant: Variant) =>
    variant === "primary" ? "button" : `button ${variant}`;

// A file entry reads as empty text.
export function formText(data: FormData, name: string) {
    const value = data.get(name);
    return typeof value === "string" ? value : "";
}

export function formTexts(data: FormData, name: string) {
    return data
        .getAll(name)
        .map((value) => (typeof value === "string" ? value : ""));
}

// Shows the server's refusal of each named field on that field.
export function FieldErrors({
    value,
    children,
}: {
    value: Record<string, string>;
    children: ReactNode;
}) {
    return <FormErrors value={value}>{children}</FormErrors>;
}

export function useErrorMessage() {
    const t = useTranslations("Common");
    return (error: unknown) => displayMessage(error, t("unexpected"));
}

function describedBy(...ids: (string | false | undefined)[]) {
    return ids.filter(Boolean).join(" ") || undefined;
}

function useControl(name: string | undefined, hint?: string) {
    const id = useId();
    const error = useContext(FormErrors)[name ?? ""];
    return {
        id,
        error,
        props: {
            id,
            name,
            "aria-invalid": error ? true : undefined,
            "aria-describedby": describedBy(
                !!error && `${id}-error`,
                !!hint && `${id}-hint`,
            ),
        },
    };
}

function Messages({
    id,
    hint,
    error,
}: {
    id: string;
    hint?: string;
    error?: string;
}) {
    return (
        <>
            {error && (
                <span className="field-error" id={`${id}-error`}>
                    {error}
                </span>
            )}
            {hint && (
                <span className="field-hint" id={`${id}-hint`}>
                    {hint}
                </span>
            )}
        </>
    );
}

export function Field({
    label,
    name,
    type = "text",
    required = false,
    hint,
    onChange,
    ...input
}: {
    label: string;
    name: string;
    type?: string;
    defaultValue?: string | number;
    value?: string;
    required?: boolean;
    min?: number;
    max?: number;
    step?: string;
    pattern?: string;
    title?: string;
    inputMode?: InputHTMLAttributes<HTMLInputElement>["inputMode"];
    placeholder?: string;
    autoComplete?: string;
    hint?: string;
    maxLength?: number;
    minLength?: number;
    onChange?: ChangeEventHandler<HTMLInputElement>;
}) {
    const control = useControl(name, hint);
    return (
        <div className="field">
            <label htmlFor={control.id}>{label}</label>
            <input
                {...input}
                {...control.props}
                type={type}
                required={required}
                onChange={onChange}
            />
            <Messages id={control.id} hint={hint} error={control.error} />
        </div>
    );
}

export function Select({
    label,
    name,
    children,
    required = false,
    hint,
    ...select
}: {
    label: string;
    name?: string;
    children: ReactNode;
    defaultValue?: string;
    value?: string;
    disabled?: boolean;
    required?: boolean;
    hint?: string;
    onChange?: ChangeEventHandler<HTMLSelectElement>;
}) {
    const control = useControl(name, hint);
    return (
        <div className="field">
            <label htmlFor={control.id}>{label}</label>
            <select {...select} {...control.props} required={required}>
                {children}
            </select>
            <Messages id={control.id} hint={hint} error={control.error} />
        </div>
    );
}

export function TextAreaField({
    label,
    name,
    required = false,
    hint,
    ...textarea
}: {
    label: string;
    name: string;
    defaultValue?: string;
    maxLength?: number;
    required?: boolean;
    hint?: string;
}) {
    const control = useControl(name, hint);
    return (
        <div className="field">
            <label htmlFor={control.id}>{label}</label>
            <textarea {...textarea} {...control.props} required={required} />
            <Messages id={control.id} hint={hint} error={control.error} />
        </div>
    );
}

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
    name,
    onChange,
    ...input
}: {
    label: string;
    name?: string;
    checked?: boolean;
    defaultChecked?: boolean;
    required?: boolean;
    disabled?: boolean;
    onChange?: (checked: boolean) => void;
}) {
    const control = useControl(name);
    return (
        <>
            <label className="check-field">
                <input
                    {...input}
                    {...control.props}
                    type="checkbox"
                    onChange={
                        onChange && ((event) => onChange(event.target.checked))
                    }
                />{" "}
                {label}
            </label>
            <Messages id={control.id} error={control.error} />
        </>
    );
}

function labelText(control: unknown): string | null {
    const label =
        control instanceof HTMLInputElement ||
        control instanceof HTMLSelectElement ||
        control instanceof HTMLTextAreaElement
            ? control.labels?.[0]
            : undefined;
    if (!label) return null;
    const copy = label.cloneNode(true) as HTMLElement;
    copy.querySelectorAll(
        "input, select, textarea, button, .field-hint, .field-error",
    ).forEach((element) => element.remove());
    return copy.textContent?.trim() || null;
}

function fieldEntries(
    form: HTMLFormElement | null,
    fields: Record<string, string>,
): FieldEntry[] {
    return Object.entries(fields)
        .filter(([, message]) => message)
        .map(([name, message]) => {
            const control = form?.elements.namedItem(name);
            return {
                name,
                message,
                label: labelText(
                    control instanceof RadioNodeList ? control[0] : control,
                ),
            };
        });
}

function FormAlert({
    alertRef,
    title,
    message,
    entries = [],
    onSelect,
}: {
    alertRef: RefObject<HTMLDivElement | null>;
    title: string;
    message: string;
    entries?: FieldEntry[];
    onSelect?: (name: string) => void;
}) {
    const repeated = entries.some((entry) => entry.message === message);
    return (
        <div
            className="notice error form-errors"
            role="alert"
            tabIndex={-1}
            ref={alertRef}
        >
            <strong>{title}</strong>
            {message && !repeated && <p>{message}</p>}
            {entries.length > 0 && (
                <ul>
                    {entries.map((entry) => (
                        <li key={entry.name}>
                            {entry.label ? (
                                <button
                                    type="button"
                                    className="text-button"
                                    onClick={() => onSelect?.(entry.name)}
                                >
                                    {`${entry.label}: ${entry.message}`}
                                </button>
                            ) : (
                                entry.message
                            )}
                        </li>
                    ))}
                </ul>
            )}
        </div>
    );
}

type FormState =
    | { status: "idle" | "pending" | "success" }
    | {
          status: "error";
          message: string;
          fields: Record<string, string>;
          entries: FieldEntry[];
      };

export function ActionForm({
    children,
    submit,
    onSubmit,
    permitted,
    errorTitle,
    success,
    variant = "primary",
    onEdit,
}: {
    children: ReactNode;
    submit: string;
    onSubmit: (data: FormData) => Promise<void>;
    onEdit?: () => void;
    permitted?: boolean;
    errorTitle?: string;
    success?: string | null;
    variant?: Variant;
}) {
    const t = useTranslations("Common");
    const errorMessage = useErrorMessage();
    const canManage = useCanManageWorkspace();
    const canSubmit = permitted ?? canManage;
    const [state, setState] = useState<FormState>({ status: "idle" });
    const running = useRef(false);
    const errorRef = useRef<HTMLDivElement>(null);
    const formRef = useRef<HTMLFormElement>(null);
    useEffect(() => {
        if (state.status === "error") errorRef.current?.focus();
    }, [state]);
    return (
        <form
            ref={formRef}
            className={canSubmit ? undefined : "readonly-form"}
            onChange={() => {
                onEdit?.();
                if (state.status === "success") setState({ status: "idle" });
            }}
            onSubmit={(event) =>
                void (async () => {
                    event.preventDefault();
                    if (!canSubmit || running.current) return;
                    running.current = true;
                    const data = new FormData(event.currentTarget);
                    setState({ status: "pending" });
                    try {
                        await onSubmit(data);
                        setState({ status: "success" });
                    } catch (error) {
                        const fields =
                            error instanceof ApiError ? error.fields : {};
                        setState({
                            status: "error",
                            message: errorMessage(error),
                            fields,
                            entries: fieldEntries(formRef.current, fields),
                        });
                    } finally {
                        running.current = false;
                    }
                })()
            }
        >
            {state.status === "error" && (
                <FormAlert
                    alertRef={errorRef}
                    title={errorTitle ?? t("saveFailed")}
                    message={state.message}
                    entries={state.entries}
                    onSelect={(name) => {
                        const field = formRef.current?.elements.namedItem(name);
                        const control =
                            field instanceof RadioNodeList ? field[0] : field;
                        if (control instanceof HTMLElement) control.focus();
                    }}
                />
            )}
            <FormErrors value={state.status === "error" ? state.fields : {}}>
                <fieldset disabled={!canSubmit || state.status === "pending"}>
                    {children}
                    {canSubmit && (
                        <div className="form-footer">
                            <button
                                className={buttonClass(variant)}
                                type="submit"
                            >
                                {state.status === "pending"
                                    ? t("saving")
                                    : submit}
                            </button>
                            <span className="form-status" aria-live="polite">
                                {state.status === "success" && success !== null
                                    ? (success ?? t("saved"))
                                    : ""}
                            </span>
                        </div>
                    )}
                </fieldset>
            </FormErrors>
        </form>
    );
}

export function AsyncButton({
    children,
    onClick,
    permitted,
    variant = "primary",
    errorTitle,
    success,
    label,
    disabled = false,
}: {
    children: string;
    onClick: () => Promise<void>;
    permitted?: boolean;
    variant?: Variant;
    errorTitle?: string;
    success?: string;
    label?: string;
    disabled?: boolean;
}) {
    const t = useTranslations("Common");
    const errorMessage = useErrorMessage();
    const announce = useAnnounce();
    const canManage = useCanManageWorkspace();
    const [state, setState] = useState<
        { status: "idle" | "pending" } | { status: "error"; message: string }
    >({ status: "idle" });
    const running = useRef(false);
    const alertRef = useRef<HTMLDivElement>(null);
    useEffect(() => {
        if (state.status === "error") alertRef.current?.focus();
    }, [state]);
    if (!(permitted ?? canManage)) return null;
    return (
        <div className="async-action">
            {state.status === "error" && (
                <FormAlert
                    alertRef={alertRef}
                    title={errorTitle ?? t("actionFailed")}
                    message={state.message}
                />
            )}
            <button
                type="button"
                className={buttonClass(variant)}
                aria-label={state.status === "pending" ? undefined : label}
                disabled={disabled || state.status === "pending"}
                onClick={() =>
                    void (async () => {
                        if (running.current) return;
                        running.current = true;
                        setState({ status: "pending" });
                        try {
                            await onClick();
                            setState({ status: "idle" });
                            if (success) announce(success);
                        } catch (error) {
                            setState({
                                status: "error",
                                message: errorMessage(error),
                            });
                        } finally {
                            running.current = false;
                        }
                    })()
                }
            >
                {state.status === "pending" ? t("working") : children}
            </button>
        </div>
    );
}
