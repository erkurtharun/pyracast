import type { components } from "./schema";

type Schemas = components["schemas"];

// Each label group in the `Enums` catalog names the generated enumeration it labels; a test keeps both in step.
export const vocabulary = {
    RegionStatus: "RegionStatusEnum",
    LocationChangeStatus: "LocationChangeStatusEnum",
    Freshness: "FreshnessEnum",
    CalibrationStatus: "CalibrationStatusEnum",
    CalibrationAssessment: "CalibrationAssessmentEnum",
    ChangeRequestKind: "ChangeRequestKindEnum",
    ChangeRequestStatus: "ChangeRequestStatusEnum",
    IncidentKind: "IncidentKindEnum",
    ObservationQuality: "ObservationQualityEnum",
    CollectionEndpoint: "CollectionEndpointEnum",
    CollectionJobStatus: "CollectionJobStatusEnum",
    CollectionJobError: "CollectionJobErrorEnum",
    NotificationDeliveryStatus: "NotificationDeliveryStatusEnum",
    NotificationDeliveryError: "NotificationDeliveryErrorEnum",
    PreparedMessageKind: "PreparedMessageKindEnum",
    ReminderState: "ReminderStateEnum",
    WorkspaceRole: "WorkspaceRoleEnum",
} as const satisfies Record<string, keyof Schemas>;

export type Vocabulary = {
    [Group in keyof typeof vocabulary]: Schemas[(typeof vocabulary)[Group]];
};
