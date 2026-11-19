import { expect, test } from "@playwright/test";
import { totp } from "../integration/support/totp";

// RFC 6238, appendix B, SHA-1 secret "12345678901234567890"; the last six digits.
const SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ";

test("an authenticator code is the code of the moment it is asked for", () => {
    expect(totp(SECRET, 59_000)).toBe("287082");
    expect(totp(SECRET, 1_111_111_109_000)).toBe("081804");
});
