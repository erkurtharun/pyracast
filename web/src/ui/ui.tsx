"use client";
import { useCanManageWorkspace } from "./workspace-access";
import { useLocale, useTranslations } from "next-intl";
import {
    createContext,
    useContext,
    useEffect,
    useId,
    useRef,
    useState,
    type ReactNode,
} from "react";
import { ApiError } from "@/lib/api/errors";
import type { Loaded } from "@/lib/api/query";
const FormErrors = createContext<Record<string, string>>({});
export function RefreshFailed({ resource }: { resource: Loaded<unknown> }) {
    const t = useTranslations("Common");
    const locale = useLocale();
    if (!resource.error || resource.data === undefined) return null;
    return (
        <div className="notice error" role="alert">
            <p>
                {t("refreshFailed", {
                    time: new Date(resource.updatedAt).toLocaleTimeString(
                        locale,
                        { timeStyle: "short" },
                    ),
                })}{" "}
                {resource.error}
            </p>
            <button
                className="button secondary"
                type="button"
                onClick={resource.retry}
            >
                {t("retry")}
            </button>
        </div>
    );
}
export function Notice({
    children,
    error = false,
}: {
    children: ReactNode;
    error?: boolean;
}) {
    return (
        <div
            className={`notice ${error ? "error" : ""}`}
            role={error ? "alert" : "status"}
        >
            {children}
        </div>
    );
}
export function Field({
    label,
    name,
    type = "text",
    defaultValue,
    required = true,
    min,
    max,
    step,
    placeholder,
    autoComplete,
    hint,
    maxLength,
    minLength,
}: {
    label: string;
    name: string;
    type?: string;
    defaultValue?: string | number;
    required?: boolean;
    min?: number;
    max?: number;
    step?: string;
    placeholder?: string;
    autoComplete?: string;
    hint?: string;
    maxLength?: number;
    minLength?: number;
}) {
    const id = useId();
    const error = useContext(FormErrors)[name];
    return (
        <div className="field">
            <label htmlFor={id}>{label}</label>
            <input
                id={id}
                name={name}
                type={type}
                defaultValue={defaultValue}
                required={required}
                min={min}
                max={max}
                step={step}
                placeholder={placeholder}
                autoComplete={autoComplete}
                maxLength={maxLength}
                minLength={minLength}
                aria-invalid={error ? true : undefined}
                aria-describedby={
                    error ? `${id}-error` : hint ? `${id}-hint` : undefined
                }
            />
            {error ? (
                <span className="field-error" id={`${id}-error`}>
                    {error}
                </span>
            ) : hint ? (
                <span className="field-hint" id={`${id}-hint`}>
                    {hint}
                </span>
            ) : null}
        </div>
    );
}
export function Select({
    label,
    name,
    children,
    defaultValue,
}: {
    label: string;
    name: string;
    children: ReactNode;
    defaultValue?: string;
}) {
    return (
        <label className="field">
            {label}
            <select name={name} defaultValue={defaultValue}>
                {children}
            </select>
        </label>
    );
}
export function ActionForm({
    children,
    submit,
    onSubmit,
    permitted,
}: {
    children: ReactNode;
    submit: string;
    onSubmit: (data: FormData) => Promise<void>;
    permitted?: boolean;
}) {
    const t = useTranslations("Common");
    const canManage = useCanManageWorkspace();
    const canSubmit = permitted ?? canManage;
    const [state, setState] = useState<
        | { status: "idle" | "pending" | "success" }
        | { status: "error"; message: string; fields: Record<string, string> }
    >({ status: "idle" });
    const errorRef = useRef<HTMLDivElement>(null);
    const formRef = useRef<HTMLFormElement>(null);
    useEffect(() => {
        if (state.status === "error") errorRef.current?.focus();
    }, [state]);
    return (
        <form
            ref={formRef}
            className={canSubmit ? undefined : "readonly-form"}
            onSubmit={async (event) => {
                event.preventDefault();
                if (!canSubmit || state.status === "pending") return;
                const data = new FormData(event.currentTarget);
                setState({ status: "pending" });
                try {
                    await onSubmit(data);
                    setState({ status: "success" });
                } catch (error) {
                    setState({
                        status: "error",
                        message:
                            error instanceof Error
                                ? error.message
                                : t("unexpected"),
                        fields: error instanceof ApiError ? error.fields : {},
                    });
                }
            }}
        >
            {state.status === "error" && (
                <div
                    className="notice error form-errors"
                    role="alert"
                    tabIndex={-1}
                    ref={errorRef}
                >
                    <strong>{t("saveFailed")}</strong>
                    <p>{state.message}</p>
                    {Object.keys(state.fields).length > 0 && (
                        <ul>
                            {Object.entries(state.fields).map(
                                ([name, message]) => (
                                    <li key={name}>
                                        <button
                                            type="button"
                                            className="text-button"
                                            onClick={() => {
                                                const field =
                                                    formRef.current?.elements.namedItem(
                                                        name,
                                                    );
                                                if (
                                                    field instanceof HTMLElement
                                                )
                                                    field.focus();
                                            }}
                                        >
                                            {message}
                                        </button>
                                    </li>
                                ),
                            )}
                        </ul>
                    )}
                </div>
            )}
            <FormErrors value={state.status === "error" ? state.fields : {}}>
                <fieldset disabled={!canSubmit || state.status === "pending"}>
                    {children}
                    {canSubmit && (
                        <div className="form-footer">
                            <button className="button" type="submit">
                                {state.status === "pending"
                                    ? t("saving")
                                    : submit}
                            </button>
                            {state.status === "success" && (
                                <span role="status">{t("saved")}</span>
                            )}
                        </div>
                    )}
                </fieldset>
            </FormErrors>
        </form>
    );
}
export function Stat({
    label,
    value,
    unit,
    note,
}: {
    label: string;
    value: unknown;
    unit?: string;
    note?: string;
}) {
    const locale = useLocale();
    return (
        <div className="stat">
            <span>{label}</span>
            <strong>
                {typeof value === "number"
                    ? new Intl.NumberFormat(locale, {
                          maximumFractionDigits: 3,
                      }).format(value)
                    : value == null
                      ? "—"
                      : String(value)}{" "}
                <small>{unit}</small>
            </strong>
            {note && <span>{note}</span>}
        </div>
    );
}
export function Tag({
    children,
    tone,
}: {
    children: ReactNode;
    tone?: "neutral" | "success" | "warning" | "danger";
}) {
    const t = useTranslations("Common");
    const value = typeof children === "string" ? children.toLowerCase() : "";
    const color =
        tone ??
        (["approved", "fresh", "ready", "active", "succeeded"].includes(value)
            ? "success"
            : ["pending", "stale", "provisional", "unknown"].includes(value)
              ? "warning"
              : ["failed", "rejected"].includes(value)
                ? "danger"
                : "neutral");
    return (
        <span className={`tag tag-${color}`}>
            {t.has(`statuses.${value}`) ? t(`statuses.${value}`) : children}
        </span>
    );
}
export function JsonDetails({
    data,
    label,
}: {
    data: unknown;
    label?: string;
}) {
    const t = useTranslations("Common");
    return (
        <details>
            <summary>{label ?? t("technicalDetails")}</summary>
            <pre>{JSON.stringify(data, null, 2)}</pre>
        </details>
    );
}
export function Empty({
    title,
    children,
}: {
    title: string;
    children: ReactNode;
}) {
    return (
        <div className="empty">
            <span className="empty-icon" aria-hidden="true">
                ☀
            </span>
            <h3>{title}</h3>
            <p>{children}</p>
        </div>
    );
}
