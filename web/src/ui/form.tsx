"use client";
import { useTranslations } from "next-intl";
import {
    createContext,
    useContext,
    useEffect,
    useId,
    useLayoutEffect,
    useRef,
    useState,
    type ChangeEventHandler,
    type ClipboardEventHandler,
    type ReactNode,
    type RefObject,
} from "react";
import { ApiError, displayMessage, UnconfirmedError } from "@/lib/api/errors";
import { writesStarted } from "@/lib/api/writes";
import { useLeaveGuard, useSubmissionGuard } from "@/lib/leave-guard";
import { useAnnounce } from "./live-region";
import { Notice } from "./ui";
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

export function useWriteErrorMessage() {
    const common = useTranslations("Common");
    const t = useTranslations("Errors");
    return (
        error: unknown,
        { fallback = common("unexpected") }: { fallback?: string } = {},
    ) =>
        error instanceof UnconfirmedError
            ? t("unconfirmed")
            : displayMessage(error, fallback);
}

function useErrorTitle() {
    const t = useTranslations("Errors");
    return (error: unknown, title: string) =>
        error instanceof UnconfirmedError ? t("unconfirmedTitle") : title;
}

export function UnconfirmedTitle({ error }: { error: unknown }) {
    const t = useTranslations("Errors");
    return error instanceof UnconfirmedError ? (
        <strong>{t("unconfirmedTitle")}</strong>
    ) : null;
}

// The notice of a write whose answer was lost, for as long as its owner keeps that write; `repeatable` only while
// the next send is that very request and can be sent.
export function UnconfirmedNotice({
    repeatable,
    lead,
}: {
    repeatable: boolean;
    lead?: string;
}) {
    const t = useTranslations("Errors");
    return (
        <Notice error>
            <strong>{t("unconfirmedTitle")}</strong>
            <p>
                {lead && `${lead} `}
                {t(repeatable ? "unconfirmedRepeatable" : "unconfirmed")}
            </p>
        </Notice>
    );
}

export function WriteFailure({
    error,
    lead,
    children,
}: {
    error: unknown;
    lead?: string;
    children?: ReactNode;
}) {
    const message = useWriteErrorMessage()(error);
    return (
        <Notice error>
            <UnconfirmedTitle error={error} />
            <p>
                {lead && `${lead} `}
                {message}
                {children}
            </p>
        </Notice>
    );
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
    placeholder?: string;
    autoComplete?: string;
    hint?: string;
    maxLength?: number;
    minLength?: number;
    onChange?: ChangeEventHandler<HTMLInputElement>;
    onPaste?: ClipboardEventHandler<HTMLInputElement>;
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

type PlainControl = { label: ReactNode; children: ReactNode };
type DescribedControl = {
    label: ReactNode;
    hint?: string;
    error?: string;
    children: (describedBy: string | undefined) => ReactNode;
};

function described(
    props: PlainControl | DescribedControl,
): props is DescribedControl {
    return typeof props.children === "function";
}

export function ControlField(props: PlainControl | DescribedControl) {
    const id = useId();
    if (!described(props))
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
                {props.children(
                    describedBy(
                        !!props.error && `${id}-error`,
                        !!props.hint && `${id}-hint`,
                    ),
                )}
            </label>
            <Messages id={id} hint={props.hint} error={props.error} />
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
    alertRef?: RefObject<HTMLDivElement | null>;
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
    | { status: "idle" | "success" }
    | { status: "pending"; since: number }
    | {
          status: "error";
          error: unknown;
          fields: Record<string, string>;
          entries: FieldEntry[];
      };

export function ActionForm({
    children,
    submit,
    onSubmit,
    permitted,
    pending,
    errorTitle,
    success,
    variant = "primary",
    onEdit,
    attempt,
    guard = true,
    unsaved = false,
}: {
    children: ReactNode;
    submit: string;
    onSubmit: (data: FormData) => Promise<void>;
    onEdit?: (data: FormData) => void;
    permitted?: boolean;
    // An owner that tracks its own request says here whether it is still unanswered, so a form mounted again
    // while it runs shows it and cannot send it twice.
    pending?: boolean;
    errorTitle?: string;
    success?: string | null;
    variant?: Variant;
    // The owner's write whose answer was lost, while it keeps it: whether a submission of `data` sends it again.
    attempt?: { repeats: (data: FormData) => boolean } | null;
    guard?: boolean;
    unsaved?: boolean;
}) {
    const t = useTranslations("Common");
    const errors = useTranslations("Errors");
    const writeErrorMessage = useWriteErrorMessage();
    const errorHeading = useErrorTitle();
    const canManage = useCanManageWorkspace();
    const canSubmit = permitted ?? canManage;
    const [state, setState] = useState<FormState>({ status: "idle" });
    // Whether the form as it now stands, valid and submittable, would send the owner's attempt.
    const [sendsRepeat, setSendsRepeat] = useState(false);
    const [edited, setEdited] = useState(false);
    const busy = pending ?? state.status === "pending";
    const submitting = state.status === "pending";
    useSubmissionGuard(guard && submitting ? state.since : null);
    useLeaveGuard(
        guard &&
            (unsaved ||
                !!attempt ||
                (!submitting && (edited || !!pending)) ||
                (state.status === "error" &&
                    state.error instanceof UnconfirmedError)),
    );
    const running = useRef(false);
    const errorRef = useRef<HTMLDivElement>(null);
    const formRef = useRef<HTMLFormElement>(null);
    const submitRef = useRef<HTMLButtonElement>(null);
    useEffect(() => {
        if (state.status === "error") errorRef.current?.focus();
    }, [state]);
    // An enclosing fieldset can disable the submit control without this form knowing.
    const submittable = () => {
        const button = submitRef.current;
        return !!button && !button.matches(":disabled");
    };
    // Read after every commit, since a field mounted or removed by its owner fires no change.
    const measure = () => {
        const form = formRef.current;
        setSendsRepeat(
            !!form &&
                !!attempt &&
                submittable() &&
                form.checkValidity() &&
                attempt.repeats(new FormData(form)),
        );
    };
    useLayoutEffect(measure);
    const repeats = !!attempt && canSubmit && !busy && sendsRepeat;
    // The owner's attempt tells its own lost answer; every other answer is told beside it.
    const answer =
        state.status === "error" &&
        !(attempt && state.error instanceof UnconfirmedError)
            ? state
            : null;
    return (
        <form
            ref={formRef}
            className={canSubmit ? undefined : "readonly-form"}
            onChange={(event) => {
                setEdited(true);
                onEdit?.(new FormData(event.currentTarget));
                if (state.status === "success") setState({ status: "idle" });
                measure();
            }}
            onSubmit={(event) =>
                void (async () => {
                    event.preventDefault();
                    if (!canSubmit || busy || running.current || !submittable())
                        return;
                    running.current = true;
                    const data = new FormData(event.currentTarget);
                    measure();
                    setState({ status: "pending", since: writesStarted() });
                    try {
                        await onSubmit(data);
                        setState({ status: "success" });
                        setEdited(false);
                    } catch (error) {
                        const fields =
                            error instanceof ApiError ? error.fields : {};
                        setState({
                            status: "error",
                            error,
                            fields,
                            entries: fieldEntries(formRef.current, fields),
                        });
                    } finally {
                        running.current = false;
                    }
                })()
            }
        >
            {answer && (
                <FormAlert
                    alertRef={errorRef}
                    title={errorHeading(
                        answer.error,
                        errorTitle ?? t("saveFailed"),
                    )}
                    message={writeErrorMessage(answer.error)}
                    entries={answer.entries}
                    onSelect={(name) => {
                        const field = formRef.current?.elements.namedItem(name);
                        const control =
                            field instanceof RadioNodeList ? field[0] : field;
                        if (control instanceof HTMLElement) control.focus();
                    }}
                />
            )}
            {attempt && (
                <FormAlert
                    alertRef={answer ? undefined : errorRef}
                    title={errors("unconfirmedTitle")}
                    message={errors(
                        repeats ? "unconfirmedRepeatable" : "unconfirmed",
                    )}
                />
            )}
            <FormErrors value={answer ? answer.fields : {}}>
                <fieldset disabled={!canSubmit || busy}>
                    {children}
                    {canSubmit && (
                        <div className="form-footer">
                            <button
                                ref={submitRef}
                                className={buttonClass(variant)}
                                type="submit"
                            >
                                {busy ? t("saving") : submit}
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
    const writeErrorMessage = useWriteErrorMessage();
    const errorHeading = useErrorTitle();
    const announce = useAnnounce();
    const canManage = useCanManageWorkspace();
    const [state, setState] = useState<
        { status: "idle" | "pending" } | { status: "error"; error: unknown }
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
                    title={errorHeading(
                        state.error,
                        errorTitle ?? t("actionFailed"),
                    )}
                    message={writeErrorMessage(state.error)}
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
                            setState({ status: "error", error });
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
