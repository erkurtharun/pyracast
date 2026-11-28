import { useEffect, useRef, useSyncExternalStore } from "react";
import { browserErrors } from "@/i18n/browser";
import { ApiError } from "./api/errors";
import { authRequest, type AuthResult } from "./auth-api";

function supported() {
    return (
        window.isSecureContext &&
        typeof PublicKeyCredential !== "undefined" &&
        typeof PublicKeyCredential.parseCreationOptionsFromJSON ===
            "function" &&
        typeof PublicKeyCredential.parseRequestOptionsFromJSON === "function" &&
        typeof PublicKeyCredential.prototype.toJSON === "function"
    );
}

const subscribe = () => () => {};

export function usePasskeySupport() {
    return useSyncExternalStore(subscribe, supported, () => false);
}

export function usePasskeySignal() {
    const controller = useRef<AbortController>(undefined);
    useEffect(() => () => controller.current?.abort(), []);
    return () => {
        controller.current?.abort();
        controller.current = new AbortController();
        return controller.current.signal;
    };
}

export const passkeyPath = "account/authenticators/webauthn";

export function passkeyCreationOptions() {
    return authRequest<{
        creation_options?: {
            publicKey?: PublicKeyCredentialCreationOptionsJSON;
        };
    }>(`${passkeyPath}?passwordless=true`, "GET");
}

export async function createPasskey(
    options: PublicKeyCredentialCreationOptionsJSON | undefined,
    signal: AbortSignal,
) {
    if (!options) throw new ApiError(browserErrors()("invalidResponse"));
    const publicKey = PublicKeyCredential.parseCreationOptionsFromJSON(options);
    return serializeCredential(
        await navigator.credentials.create({ publicKey, signal }),
        signal,
    );
}

export type PasskeyAssertion = "login" | "authenticate" | "reauthenticate";

export async function assertPasskey(
    action: PasskeyAssertion,
    signal: AbortSignal,
    onAutofillSelection?: () => void,
): Promise<AuthResult> {
    const path = `auth/webauthn/${action}`;
    const options = await authRequest<{
        request_options?: { publicKey?: PublicKeyCredentialRequestOptionsJSON };
    }>(
        onAutofillSelection ? `${path}?autofill` : path,
        "GET",
        undefined,
        signal,
    );
    signal.throwIfAborted();
    if (options.status === 401) return options;
    const json = options.data?.request_options?.publicKey;
    if (!json) throw new ApiError(browserErrors()("invalidResponse"));
    const publicKey = PublicKeyCredential.parseRequestOptionsFromJSON(json);
    const credential = serializeCredential(
        await navigator.credentials.get({
            publicKey,
            signal,
            ...(onAutofillSelection && { mediation: "conditional" }),
        }),
        signal,
    );
    onAutofillSelection?.();
    const verify = onAutofillSelection
        ? `${path}?autofill=${encodeURIComponent(json.challenge)}`
        : path;
    return authRequest(verify, "POST", { credential }, signal);
}

function serializeCredential(
    credential: Credential | null,
    signal: AbortSignal,
) {
    signal.throwIfAborted();
    if (!(credential instanceof PublicKeyCredential))
        throw new ApiError(browserErrors()("invalidResponse"));
    return credential.toJSON();
}

export function cancelledPasskey(error: unknown) {
    return error instanceof DOMException && error.name === "NotAllowedError";
}
