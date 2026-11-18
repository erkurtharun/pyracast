import { createHmac } from "node:crypto";

export function totp(secret: string): string {
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    let bits = "";
    for (const character of secret.replace(/=+$/, "").toUpperCase()) {
        const value = alphabet.indexOf(character);
        if (value < 0) throw new Error("Invalid authenticator setup key");
        bits += value.toString(2).padStart(5, "0");
    }
    const bytes: number[] = [];
    for (let index = 0; index + 8 <= bits.length; index += 8)
        bytes.push(parseInt(bits.slice(index, index + 8), 2));
    const counter = Buffer.alloc(8);
    counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000)));
    const digest = createHmac("sha1", Buffer.from(bytes))
        .update(counter)
        .digest();
    const offset = digest.readUInt8(digest.length - 1) & 15;
    return String(
        (digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000,
    ).padStart(6, "0");
}
