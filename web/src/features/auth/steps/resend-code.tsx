"use client";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { ApiError } from "@/lib/api/errors";
import { useErrorMessage } from "@/ui/form";
import { authRequest } from "@/lib/auth-api";

// Matches the confirm_email rate limit of one code per minute.
const RESEND_SECONDS = 60;
const RESEND_MILLISECONDS = RESEND_SECONDS * 1000;

function secondsUntil(availableAt: number) {
    return Math.max(0, Math.ceil((availableAt - Date.now()) / 1000));
}

export function ResendCode({
    sentAt,
    onSent,
}: {
    sentAt: number;
    onSent: () => void;
}) {
    const t = useTranslations("Auth");
    const errorMessage = useErrorMessage();
    const [availableAt, setAvailableAt] = useState(
        sentAt && sentAt + RESEND_MILLISECONDS,
    );
    const [seconds, setSeconds] = useState(() => secondsUntil(availableAt));
    const [state, setState] = useState<
        | { status: "idle" | "pending" }
        | { status: "error"; message: string; final: boolean }
    >({ status: "idle" });
    useEffect(() => {
        if (!availableAt) return;
        const timer = window.setInterval(() => {
            const remaining = secondsUntil(availableAt);
            setSeconds(remaining);
            if (!remaining) window.clearInterval(timer);
        }, 1000);
        return () => window.clearInterval(timer);
    }, [availableAt]);

    async function resend() {
        setState({ status: "pending" });
        try {
            const result = await authRequest(
                "auth/email/verify/resend",
                "POST",
                {},
            );
            if (result.status === 200) {
                onSent();
                return;
            }
            setState({
                status: "error",
                message: t("resendLimit"),
                final: true,
            });
        } catch (error) {
            const status = error instanceof ApiError ? error.status : undefined;
            if (status === 429) {
                setAvailableAt(Date.now() + RESEND_MILLISECONDS);
                setSeconds(RESEND_SECONDS);
            }
            setState({
                status: "error",
                message:
                    status === 429
                        ? t("resendWait")
                        : status === 409
                          ? t("resendLimit")
                          : errorMessage(error),
                final: status === 409,
            });
        }
    }

    return (
        <form
            onSubmit={(event) => {
                event.preventDefault();
                void resend();
            }}
        >
            {state.status === "error" && (
                <p className="notice error" role="alert">
                    {state.message}
                </p>
            )}
            <div className="form-footer">
                <button
                    className="button"
                    type="submit"
                    disabled={
                        seconds > 0 ||
                        state.status === "pending" ||
                        (state.status === "error" && state.final)
                    }
                >
                    {seconds > 0 ? t("resendIn", { seconds }) : t("resendCode")}
                </button>
            </div>
        </form>
    );
}
