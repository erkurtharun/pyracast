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
import { ApiError, request, type Page } from "@/lib/api";
const FormErrors = createContext<Record<string, string>>({});
export function useCollection<T>(url: string, refresh = 0, pollMs = 0) {
    const t = useTranslations("Common");
    const [state, setState] = useState<{
        url: string;
        data?: Page<T>;
        error?: string;
        loading: boolean;
    }>({ url, loading: true });
    useEffect(() => {
        let current = true;
        let sequence = 0;
        async function load(requestSequence: number) {
            const first = await request<Page<T>>(url);
            const rows = [...first.results];
            let next = first.next;
            const visited = new Set<string>();
            while (next && current && requestSequence === sequence) {
                const target = new URL(next, window.location.origin);
                const path = target.pathname + target.search;
                if (visited.has(path) || !target.pathname.startsWith("/api/"))
                    throw new Error(t("invalidPage"));
                visited.add(path);
                const page = await request<Page<T>>(path);
                rows.push(...page.results);
                next = page.next;
            }
            if (current && requestSequence === sequence)
                setState({
                    url,
                    data: { ...first, results: rows, next: null },
                    loading: false,
                });
        }
        const start = () => {
            const requestSequence = ++sequence;
            load(requestSequence).catch((error: Error) => {
                if (current && requestSequence === sequence)
                    setState((previous) => ({
                        url,
                        data: previous.url === url ? previous.data : undefined,
                        error: error.message,
                        loading: false,
                    }));
            });
        };
        start();
        const timer = pollMs > 0 ? setInterval(start, pollMs) : undefined;
        return () => {
            current = false;
            if (timer) clearInterval(timer);
        };
    }, [url, refresh, pollMs, t]);
    return state.url === url
        ? state
        : { url, data: undefined, error: undefined, loading: true };
}
export function useResource<T>(url: string, refresh = 0, pollMs = 0) {
    const locale = useLocale();
    const [state, setState] = useState<{
        data?: T;
        error?: string;
        loading: boolean;
    }>({ loading: true });
    useEffect(() => {
        let current = true;
        let sequence = 0;
        const load = () => {
            const requestSequence = ++sequence;
            request<T>(url)
                .then((data) => {
                    if (current && requestSequence === sequence)
                        setState({ data, loading: false });
                })
                .catch((error: Error) => {
                    if (current && requestSequence === sequence)
                        setState({ error: error.message, loading: false });
                });
        };
        load();
        const timer = pollMs > 0 ? setInterval(load, pollMs) : undefined;
        return () => {
            current = false;
            if (timer) clearInterval(timer);
        };
    }, [url, refresh, pollMs, locale]);
    return state;
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
                    {Object.keys(state.fields).length ? (
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
                    ) : (
                        <p>{state.message}</p>
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
