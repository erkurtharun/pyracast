"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { ApiError, displayMessage, UnconfirmedError } from "@/lib/api/errors";
import { authRequest, type AuthResult } from "@/lib/auth-api";
import {
    cancelledPasskey,
    createPasskey,
    passkeyCreationOptions,
    passkeyPath,
    usePasskeySignal,
    usePasskeySupport,
} from "@/lib/passkeys";
import { ActionForm, AsyncButton, Field } from "@/ui/form";
import { Notice } from "@/ui/ui";
import { useOneAction } from "./one-action";
import { RecoveryCodes } from "./recovery-codes";
import { refreshExpiredSession } from "./security-reauthentication";
import { StepUpPrompt, stepUpMode, useStepUp } from "./step-up";
import { ConfirmStep } from "./two-factor/confirm-step";

type Passkey = { id: number; name: string };
type Inventory =
    | { kind: "loading" | "failed" }
    | { kind: "ready"; keys: Passkey[]; totp: boolean; recovery: boolean };
type Recovery = { kind: "confirm" } | { kind: "codes"; values: string[] };

async function readInventory(): Promise<Extract<Inventory, { kind: "ready" }>> {
    const result = await authRequest<
        { type?: string; id?: number; name?: string }[]
    >("account/authenticators", "GET");
    if (!Array.isArray(result.data)) throw new Error();
    const keys = result.data.flatMap((entry) =>
        entry.type === "webauthn" &&
        typeof entry.id === "number" &&
        typeof entry.name === "string"
            ? [{ id: entry.id, name: entry.name }]
            : [],
    );
    return {
        kind: "ready",
        keys,
        totp: result.data.some((entry) => entry.type === "totp"),
        recovery: result.data.some((entry) => entry.type === "recovery_codes"),
    };
}

type CustomerSettings = {
    email: string;
    totpRevision: number;
};

export function PasskeySettings({
    mfaRequired,
    ...customer
}: CustomerSettings & { mfaRequired: boolean }) {
    const t = useTranslations("Passkeys");
    const title = useRef<HTMLHeadingElement>(null);
    return (
        <section className="panel narrow" aria-labelledby="account-passkeys">
            <h2 ref={title} id="account-passkeys" tabIndex={-1}>
                {t("title")}
            </h2>
            {mfaRequired ? (
                <p>{t("staffPolicy")}</p>
            ) : (
                <CustomerPasskeys
                    {...customer}
                    focusTitle={() => title.current?.focus()}
                />
            )}
        </section>
    );
}

function CustomerPasskeys({
    email,
    totpRevision,
    focusTitle,
}: CustomerSettings & { focusTitle: () => void }) {
    const t = useTranslations("Passkeys");
    const security = useTranslations("Security");
    const common = useTranslations("Common");
    const supported = usePasskeySupport();
    const signalForPasskey = usePasskeySignal();
    const stepUp = useStepUp();
    const { acting, run } = useOneAction();
    const [inventory, setInventory] = useState<Inventory>({ kind: "loading" });
    const [loadAttempt, setLoadAttempt] = useState(0);
    const [recovery, setRecovery] = useState<Recovery>();
    const [notice, setNotice] = useState<string>();
    const [error, setError] = useState<string>();
    const codesHeading = useRef<HTMLHeadingElement>(null);

    useEffect(() => {
        let active = true;
        readInventory()
            .then((loaded) => {
                if (active) {
                    setInventory(loaded);
                    if (loaded.totp)
                        setRecovery((current) =>
                            current?.kind === "confirm" ? undefined : current,
                        );
                }
            })
            .catch(() => {
                if (active) setInventory({ kind: "failed" });
            });
        return () => {
            active = false;
        };
    }, [loadAttempt, totpRevision]);

    useEffect(() => {
        if (recovery && !stepUp.pending) codesHeading.current?.focus();
    }, [recovery, stepUp.pending]);

    async function confirmed(
        result: AuthResult<unknown>,
        resume: () => Promise<void>,
    ) {
        if (result.status !== 401) return true;
        const mode = stepUpMode(result);
        if (!mode) await refreshExpiredSession(security("sessionExpired"));
        else stepUp.ask(mode, resume);
        return false;
    }

    async function update(operation: () => Promise<void>) {
        await run(async () => {
            setError(undefined);
            setNotice(undefined);
            try {
                await operation();
            } catch (failure) {
                setError(
                    cancelledPasskey(failure)
                        ? t("cancelled")
                        : displayMessage(failure, t("failed")),
                );
                if (failure instanceof UnconfirmedError) {
                    const loaded = await readInventory().catch(() => undefined);
                    if (loaded) setInventory(loaded);
                }
            }
        });
    }

    function add(name: string): Promise<void> {
        return update(async () => {
            const signal = signalForPasskey();
            const options = await passkeyCreationOptions();
            if (signal.aborted) return;
            if (!(await confirmed(options, () => add(name)))) return;
            const credential = await createPasskey(
                options.data?.creation_options?.publicKey,
                signal,
            );
            const result = await authRequest<Passkey>(passkeyPath, "POST", {
                credential,
                name,
            });
            if (!(await confirmed(result, () => add(name)))) return;
            const values = result.meta?.recovery_codes;
            if (values?.length) setRecovery({ kind: "codes", values });
            setNotice(t("added"));
            const key = result.data;
            if (
                !key ||
                typeof key.id !== "number" ||
                typeof key.name !== "string"
            )
                throw new ApiError(t("loadFailed"));
            setInventory((current) =>
                current.kind === "ready"
                    ? {
                          ...current,
                          keys: [...current.keys, key],
                          recovery: current.recovery || !!values?.length,
                      }
                    : current,
            );
        });
    }

    function remove(id: number): Promise<void> {
        return update(async () => {
            const result = await authRequest(passkeyPath, "DELETE", {
                authenticators: [id],
            });
            if (!(await confirmed(result, () => remove(id)))) return;
            setInventory((current) =>
                current.kind === "ready"
                    ? {
                          ...current,
                          keys: current.keys.filter((key) => key.id !== id),
                          recovery:
                              current.recovery &&
                              (current.totp || current.keys.length > 1),
                      }
                    : current,
            );
            setNotice(t("removed"));
        });
    }

    function replaceCodes(): Promise<void> {
        return update(async () => {
            const result = await authRequest(
                "account/authenticators/recovery-codes",
                "POST",
                {},
            );
            if (!(await confirmed(result, replaceCodes))) return;
            const values = result.data?.unused_codes;
            if (!values?.length) throw new ApiError(t("loadFailed"));
            setInventory((current) =>
                current.kind === "ready"
                    ? { ...current, recovery: true }
                    : current,
            );
            setRecovery({ kind: "codes", values });
            setNotice(security("codesReplaced"));
        });
    }

    const codes = recovery?.kind === "codes" ? recovery.values : undefined;
    return (
        <>
            {inventory.kind === "loading" && (
                <p role="status">{t("loading")}</p>
            )}
            {error && (
                <Notice error focus>
                    {error}
                </Notice>
            )}
            {notice && <Notice>{notice}</Notice>}
            <StepUpPrompt stepUp={stepUp} email={email} onError={setError} />
            {inventory.kind === "failed" && (
                <>
                    <Notice error>{t("loadFailed")}</Notice>
                    <AsyncButton
                        permitted
                        variant="secondary"
                        onClick={() => {
                            setInventory({ kind: "loading" });
                            setLoadAttempt((attempt) => attempt + 1);
                            return Promise.resolve();
                        }}
                    >
                        {common("retry")}
                    </AsyncButton>
                </>
            )}
            {!stepUp.pending && recovery?.kind === "confirm" && (
                <ConfirmStep
                    headingRef={codesHeading}
                    action="replace"
                    busy={acting}
                    working={acting}
                    onConfirm={() => void replaceCodes()}
                    onCancel={() => {
                        setRecovery(undefined);
                        focusTitle();
                    }}
                />
            )}
            {codes && (
                <RecoveryCodes
                    headingRef={codesHeading}
                    codes={codes}
                    busy={false}
                    onCopy={() => {
                        if (!navigator.clipboard?.writeText) {
                            setError(security("copyFailed"));
                            return;
                        }
                        void navigator.clipboard
                            .writeText(codes.join("\n"))
                            .then(
                                () => setNotice(security("codesCopied")),
                                () => setError(security("copyFailed")),
                            );
                    }}
                    onDownload={() => {
                        const file = new Blob([`${codes.join("\n")}\n`], {
                            type: "text/plain;charset=utf-8",
                        });
                        const url = URL.createObjectURL(file);
                        const link = document.createElement("a");
                        link.href = url;
                        link.download = "pyracast-recovery-codes.txt";
                        document.body.append(link);
                        link.click();
                        link.remove();
                        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
                    }}
                    onAcknowledge={() => {
                        setRecovery(undefined);
                        focusTitle();
                    }}
                />
            )}
            {inventory.kind === "ready" && !stepUp.pending && !recovery && (
                <fieldset disabled={acting}>
                    {inventory.keys.length ? (
                        <>
                            <p>{t("codeDescription")}</p>
                            {!inventory.totp &&
                                inventory.recovery &&
                                inventory.keys.length === 1 && (
                                    <p>{t("removeLast")}</p>
                                )}
                            <ul>
                                {inventory.keys.map(({ id, name }) => (
                                    <li key={id}>
                                        <p>{name}</p>
                                        <AsyncButton
                                            permitted
                                            label={t("remove", { name })}
                                            variant="secondary"
                                            onClick={() => remove(id)}
                                        >
                                            {t("removeAction")}
                                        </AsyncButton>
                                    </li>
                                ))}
                            </ul>
                            {!inventory.totp && (
                                <div className="button-group">
                                    <button
                                        className="button secondary"
                                        type="button"
                                        onClick={() =>
                                            setRecovery({ kind: "confirm" })
                                        }
                                    >
                                        {security(
                                            inventory.recovery
                                                ? "replaceRecovery"
                                                : "generateRecovery",
                                        )}
                                    </button>
                                </div>
                            )}
                        </>
                    ) : (
                        <p>{t("empty")}</p>
                    )}
                    {supported ? (
                        <ActionForm
                            guard={false}
                            permitted
                            submit={t("add")}
                            success={null}
                            onSubmit={async (data) => {
                                const name = data.get("name");
                                await add(
                                    typeof name === "string" ? name.trim() : "",
                                );
                            }}
                        >
                            <Field
                                label={t("name")}
                                name="name"
                                maxLength={100}
                            />
                        </ActionForm>
                    ) : (
                        <p>{t("unsupported")}</p>
                    )}
                </fieldset>
            )}
        </>
    );
}
