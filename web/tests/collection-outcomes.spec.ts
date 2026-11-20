import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";
import { collectionOutcomeMessages } from "../src/lib/collection-outcomes";

const web = path.join(__dirname, "..");

function read(relative: string) {
    return JSON.parse(readFileSync(path.join(web, relative), "utf8"));
}

test("every generated collection outcome has a label in both languages", () => {
    const codes: string[] =
        read("openapi.json").components.schemas.CollectionJobErrorEnum.enum;
    expect(Object.keys(collectionOutcomeMessages).sort()).toEqual(
        [...codes].sort(),
    );
    for (const language of ["en", "tr"]) {
        const messages = read(`messages/${language}/workspace.json`)
            .CollectionSchedule as Record<string, string>;
        for (const key of Object.values(collectionOutcomeMessages))
            expect(messages[key], `${language}: ${key}`).toBeTruthy();
    }
});
