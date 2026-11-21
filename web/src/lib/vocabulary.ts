import type { components } from "./schema";

type Schemas = components["schemas"];

// Each label group in the `Enums` catalog names the generated enumeration it labels; a test keeps both in step.
export const vocabulary = {
    RegionStatus: "RegionStatusEnum",
    LocationChangeStatus: "LocationChangeStatusEnum",
    Freshness: "FreshnessEnum",
    CalibrationStatus: "CalibrationStatusEnum",
    CalibrationAssessment: "CalibrationAssessmentEnum",
    SelectionState: "SelectionStateEnum",
    ActivationBlocker: "ActivationBlockerEnum",
    AutomationCause: "AutomationCauseEnum",
    ChangeRequestKind: "ChangeRequestKindEnum",
    ChangeRequestStatus: "ChangeRequestStatusEnum",
    IncidentKind: "IncidentKindEnum",
    IncidentStatus: "IncidentStatusEnum",
    ObservationQuality: "ObservationQualityEnum",
    CollectionEndpoint: "CollectionEndpointEnum",
    CollectionJobStatus: "CollectionJobStatusEnum",
    CollectionJobError: "CollectionJobErrorEnum",
    CollectionState: "CollectionStateEnum",
    CollectionInactiveReason: "CollectionInactiveReasonEnum",
    NotificationDeliveryStatus: "NotificationDeliveryStatusEnum",
    NotificationDeliveryError: "NotificationDeliveryErrorEnum",
    PreparedMessageKind: "PreparedMessageKindEnum",
    ReminderState: "ReminderStateEnum",
    WorkspaceRole: "WorkspaceRoleEnum",
    InvitationState: "InvitationStateEnum",
} as const satisfies Record<string, keyof Schemas>;

export type Vocabulary = {
    [Group in keyof typeof vocabulary]: Schemas[(typeof vocabulary)[Group]];
};
