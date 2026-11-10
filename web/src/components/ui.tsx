"use client";
import { useEffect, useState, type ReactNode } from "react";
import { request, type Page } from "@/lib/api";
export function useCollection<T>(url: string, refresh = 0) {
    const [state, setState] = useState<{
        data?: Page<T>;
        error?: string;
        loading: boolean;
    }>({ loading: true });
    useEffect(() => {
        let current = true;
        async function load() {
            const first = await request<Page<T>>(url);
            const rows = [...first.results];
            let next = first.next;
            const visited = new Set<string>();
            while (next && current) {
                const target = new URL(next, window.location.origin);
                const path = target.pathname + target.search;
                if (visited.has(path) || !target.pathname.startsWith("/api/"))
                    throw new Error(
                        "The server returned an invalid next page.",
                    );
                visited.add(path);
                const page = await request<Page<T>>(path);
                rows.push(...page.results);
                next = page.next;
            }
            if (current)
                setState({
                    data: { ...first, results: rows, next: null },
                    loading: false,
                });
        }
        load().catch((error: Error) => {
            if (current) setState({ error: error.message, loading: false });
        });
        return () => {
            current = false;
        };
    }, [url, refresh]);
    return state;
}
export function useResource<T>(url: string, refresh = 0) {
    const [state, setState] = useState<{
        data?: T;
        error?: string;
        loading: boolean;
    }>({ loading: true });
    useEffect(() => {
        let current = true;
        request<T>(url)
            .then((data) => {
                if (current) setState({ data, loading: false });
            })
            .catch((error: Error) => {
                if (current) setState({ error: error.message, loading: false });
            });
        return () => {
            current = false;
        };
    }, [url, refresh]);
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
}) {
    return (
        <label className="field">
            {label}
            <input
                name={name}
                type={type}
                defaultValue={defaultValue}
                required={required}
                min={min}
                max={max}
                step={step}
                placeholder={placeholder}
            />
        </label>
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
}: {
    children: ReactNode;
    submit: string;
    onSubmit: (data: FormData) => Promise<void>;
}) {
    const [pending, setPending] = useState(false),
        [error, setError] = useState(""),
        [success, setSuccess] = useState(false);
    return (
        <form
            onSubmit={async (event) => {
                event.preventDefault();
                const data = new FormData(event.currentTarget);
                setPending(true);
                setError("");
                setSuccess(false);
                try {
                    await onSubmit(data);
                    setSuccess(true);
                } catch (error) {
                    setError(
                        error instanceof Error
                            ? error.message
                            : "Something went wrong.",
                    );
                } finally {
                    setPending(false);
                }
            }}
        >
            <fieldset disabled={pending}>
                {children}
                <div className="form-footer">
                    <button className="button" type="submit">
                        {pending ? "Saving…" : submit}
                    </button>
                    {success && <span role="status">Saved successfully.</span>}
                </div>
            </fieldset>
            {error && <Notice error>{error}</Notice>}
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
    return (
        <div className="stat">
            <span>{label}</span>
            <strong>
                {typeof value === "number"
                    ? new Intl.NumberFormat("en", {
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
export function Tag({ children }: { children: ReactNode }) {
    return <span className="tag">{children}</span>;
}
export function JsonDetails({
    data,
    label = "Technical details",
}: {
    data: unknown;
    label?: string;
}) {
    return (
        <details>
            <summary>{label}</summary>
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
