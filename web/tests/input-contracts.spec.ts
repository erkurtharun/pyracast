import { expect, test } from "@playwright/test";
import { configurationInput } from "../src/features/portfolio/plant-configuration-fields";
import { qualityOf } from "../src/lib/production";

for (const field of ["measurement_basis", "mounting_type"]) {
    for (const value of [null, "unrecognized"]) {
        test(`configuration requires a recognized ${field} when its value is ${value}`, () => {
            const data = new FormData();
            data.set("measurement_basis", "net_export");
            data.set("mounting_type", "unknown");
            if (value === null) data.delete(field);
            else data.set(field, value);
            expect(() => configurationInput(data)).toThrow(
                "Select a valid plant configuration.",
            );
        });
    }
}

test("only an absent observation receives the default production quality", () => {
    expect(qualityOf(null)).toBe("valid");
    expect(qualityOf(undefined)).toBe("valid");
    expect(qualityOf("invalid")).toBe("invalid");
    expect(() => qualityOf("unrecognized")).toThrow(
        "Unrecognized production quality.",
    );
});
