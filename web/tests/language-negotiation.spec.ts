import { test, expect } from "@playwright/test";
import {
    languageCookie,
    languageCookieOptions,
    negotiatedLanguage,
} from "../src/i18n/request-language";
import negotiation from "./support/language-negotiation.json";

type Case = {
    cookie?: string;
    acceptLanguage: string;
    padding?: number;
    country?: string;
    expected: string;
};

test("the page language follows the server's case table", () => {
    for (const entry of negotiation.cases as Case[]) {
        const header = " ".repeat(entry.padding ?? 0) + entry.acceptLanguage;
        expect(
            negotiatedLanguage(entry.cookie, header, entry.country),
            JSON.stringify(entry),
        ).toBe(entry.expected);
    }
});

test("the language cookie has the server's attributes", () => {
    const { name, sameSite, ...attributes } = negotiation.cookie;
    expect(languageCookie).toBe(name);
    expect(languageCookieOptions(attributes.secure)).toEqual({
        ...attributes,
        sameSite: sameSite.toLowerCase(),
    });
    expect(languageCookieOptions(false).secure).toBe(false);
});
