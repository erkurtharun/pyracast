import path from "node:path";
import { expect, test } from "@playwright/test";
import { keyUse, unusedKeys } from "./support/message-keys";
import { messages } from "../src/i18n/messages";

const web = path.resolve();

test("every message key is read by a screen or names a code the server or a screen sends", () => {
    expect(unusedKeys(web, messages.en)).toEqual([]);
});

test("every translation call names its namespace and keys in a form the key check reads", () => {
    expect(keyUse(web).unresolved).toEqual([]);
});
