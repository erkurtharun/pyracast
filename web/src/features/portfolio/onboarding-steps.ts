export type OnboardingStep = "location" | "technical" | "review";

const technicalFields = [
    "dc_capacity_mw",
    "inverter_capacity_mw",
    "export_limit_mw",
    "measurement_basis",
    "mounting_type",
    "surface_tilt_degrees",
    "surface_azimuth_degrees",
];

// Completing the setup of a stored location keeps its location, so it has no location step.
export function onboardingSteps(existing: boolean): OnboardingStep[] {
    return existing
        ? ["technical", "review"]
        : ["location", "technical", "review"];
}

// The fields each step shows, in form order; the name moves with the step that asks for it.
function stepFields(existing: boolean): Record<OnboardingStep, string[]> {
    return existing
        ? {
              location: [],
              technical: ["name", ...technicalFields],
              review: [],
          }
        : {
              location: ["name", "timezone", "latitude", "longitude"],
              technical: technicalFields,
              review: [],
          };
}

export function refusalTarget(
    refused: readonly string[],
    existing: boolean,
): { step: number; field: string } | null {
    const fields = stepFields(existing);
    for (const [step, name] of onboardingSteps(existing).entries()) {
        const field = fields[name].find((candidate) =>
            refused.includes(candidate),
        );
        if (field) return { step, field };
    }
    return null;
}

// Refused fields no step shows, with their messages, for the general message; a message that
// repeats the general one is left out.
export function unplacedFields(
    refused: Readonly<Record<string, string>>,
    existing: boolean,
    general: string,
): [string, string][] {
    const shown = new Set(Object.values(stepFields(existing)).flat());
    return Object.entries(refused).filter(
        ([field, message]) => !shown.has(field) && message !== general,
    );
}
