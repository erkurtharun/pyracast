export type OnboardingStep = "location" | "technical" | "review";

const locationFields = new Set(["name", "latitude", "longitude", "timezone"]);

// Completing the setup of a stored location keeps its location, so it has no location step.
export function onboardingSteps(existing: boolean): OnboardingStep[] {
    return existing
        ? ["technical", "review"]
        : ["location", "technical", "review"];
}

export function stepOfField(
    fields: readonly string[],
    existing: boolean,
): number | null {
    if (!fields.length) return null;
    const steps = onboardingSteps(existing);
    const held = fields.map((field) =>
        steps.indexOf(
            !existing && locationFields.has(field) ? "location" : "technical",
        ),
    );
    return Math.min(...held);
}
