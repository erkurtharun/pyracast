import { readFile } from "node:fs/promises";
import { expect } from "@playwright/test";

type MailedCode = { email: string; template: string; code: string };

export async function mailedCode(email: string, template: string) {
    let code = "";
    await expect
        .poll(async () => {
            try {
                const messages = JSON.parse(
                    await readFile(process.env.BROWSER_AUTH_MAILBOX!, "utf8"),
                ) as MailedCode[];
                code =
                    messages.findLast(
                        (message) =>
                            message.email === email &&
                            message.template.includes(template),
                    )?.code ?? "";
            } catch {
                /* The backend may be rewriting the mailbox while it is read. */
            }
            return !!code;
        })
        .toBe(true);
    return code;
}
