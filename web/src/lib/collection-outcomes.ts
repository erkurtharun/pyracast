import type { components } from "@/lib/schema";

export type CollectionOutcome = components["schemas"]["CollectionJobErrorEnum"];

// Message keys in the CollectionSchedule namespace; the record type makes every stored
// outcome code a compile-time requirement.
export const collectionOutcomeMessages: Record<CollectionOutcome, string> = {
    history_window_closed: "morningRetryWindowEnded",
    attempts_exhausted: "retryLimitReached",
    forecast_superseded: "superseded",
    binding_not_current: "bindingNotCurrent",
    schedule_disabled: "scheduleDisabled",
    slot_not_configured: "slotNotConfigured",
    schedule_changed: "scheduleChanged",
    history_incomplete: "historicalDataIsIncomplete",
    history_already_archived: "historyAlreadyArchived",
    provider_authentication_failed: "credentialAttention",
    provider_allowance_exhausted: "providerAllowanceExhausted",
    provider_access_denied: "providerAccessDenied",
    provider_site_not_found: "providerSiteNotFound",
    provider_throttled: "providerThrottle",
    provider_server_error: "providerServerError",
    provider_unreachable: "providerUnreachable",
    provider_invalid_response: "unexpectedProviderData",
    provider_configuration_invalid: "providerConfigurationInvalid",
    account_unavailable: "accountUnavailable",
    history_budget_reserved: "remainingBudgetIsReservedForForecasts",
    quota_exhausted: "dailyAccountBudgetExhausted",
    provider_failure: "providerRequestFailed",
    provider_unexpected_failure: "collectionNotCompleted",
    invalid_source_kind: "unexpectedProviderData",
    calculation_failure: "forecastCalculationFailed",
    lock_contention: "systemBusy",
    publication_failure: "publicationFailed",
};
