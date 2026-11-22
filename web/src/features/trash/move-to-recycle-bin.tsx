"use client";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { RegionScope } from "@/lib/api";
import { api } from "@/lib/api/client";
import { useErrorMessage } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useRememberDeletion } from "./recent-deletion";

type Step =
    | { kind: "idle" }
    | { kind: "confirming" }
    | { kind: "working" }
    | { kind: "failed"; message: string };

export function MoveToRecycleBin({
    name,
    scope,
    onDeleted,
}: {
    name: string;
    scope: RegionScope;
    onDeleted: () => void;
}) {
    const t = useTranslations("RecycleBin");
    const errorMessage = useErrorMessage();
    const remember = useRememberDeletion();
    const [step, setStep] = useState<Step>({ kind: "idle" });
    const heading = useRef<HTMLHeadingElement>(null);
    const confirming = step.kind === "confirming";
    useEffect(() => {
        if (confirming) heading.current?.focus();
    }, [confirming]);
    if (step.kind === "idle")
        return (
            <button
                className="button secondary"
                onClick={() => setStep({ kind: "confirming" })}
            >
                {t("deletePlant")}
            </button>
        );
    const working = step.kind === "working";
    return (
        <section className="notice" aria-label={t("deletePlantConfirmation")}>
            <h2 ref={heading} tabIndex={-1}>
                {t("confirmMove", { name })}
            </h2>
            <p>{t("deletionHelp")}</p>
            {step.kind === "failed" && <Notice error>{step.message}</Notice>}
            <div className="button-group">
                <button
                    className="button secondary"
                    disabled={working}
                    onClick={() => setStep({ kind: "idle" })}
                >
                    {t("cancel")}
                </button>
                <button
                    className="button danger"
                    disabled={working}
                    onClick={() =>
                        void (async () => {
                            setStep({ kind: "working" });
                            try {
                                await api.delete(
                                    "/api/tenants/{tenant_id}/regions/{region_id}/",
                                    { path: scope },
                                );
                            } catch (failure) {
                                setStep({
                                    kind: "failed",
                                    message: errorMessage(failure),
                                });
                                return;
                            }
                            remember({
                                tenant: scope.tenant_id,
                                region: scope.region_id,
                                name,
                            });
                            onDeleted();
                        })()
                    }
                >
                    {working ? t("moving") : t("movePlantToRecycleBin")}
                </button>
            </div>
        </section>
    );
}
