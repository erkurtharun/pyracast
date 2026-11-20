import { useEffect, useRef, type ReactNode } from "react";

export function ViewNav<K extends string>({
    label,
    views,
    current,
    onSelect,
    tabs = false,
    busy = false,
}: {
    label: string;
    views: readonly (readonly [K, string])[];
    current: K;
    onSelect: (view: K) => void;
    tabs?: boolean;
    busy?: boolean;
}) {
    return (
        <nav
            className={tabs ? "tabs" : "subnav"}
            aria-label={label}
            aria-busy={busy || undefined}
        >
            {views.map(([key, text]) => (
                <button
                    key={key}
                    type="button"
                    className={current === key ? "active" : ""}
                    aria-current={current === key ? "page" : undefined}
                    onFocus={
                        tabs
                            ? (event) =>
                                  event.currentTarget.scrollIntoView({
                                      block: "nearest",
                                      inline: "nearest",
                                  })
                            : undefined
                    }
                    onClick={() => onSelect(key)}
                >
                    {text}
                </button>
            ))}
        </nav>
    );
}

export function ConfirmAction({
    label,
    children,
    cancel,
    confirm,
    confirming,
    pending,
    onCancel,
    onConfirm,
}: {
    label: string;
    children: ReactNode;
    cancel: string;
    confirm: string;
    confirming: string;
    pending: boolean;
    onCancel: () => void;
    onConfirm: () => void;
}) {
    const cancelRef = useRef<HTMLButtonElement>(null);
    useEffect(() => cancelRef.current?.focus(), []);
    return (
        <section className="notice" aria-label={label}>
            {children}
            <div className="button-group">
                <button
                    type="button"
                    className="button secondary"
                    ref={cancelRef}
                    disabled={pending}
                    onClick={onCancel}
                >
                    {cancel}
                </button>
                <button
                    type="button"
                    className="button"
                    disabled={pending}
                    onClick={onConfirm}
                >
                    {pending ? confirming : confirm}
                </button>
            </div>
        </section>
    );
}
