import { expect, test } from "@playwright/test";
import { csvDocument } from "../src/lib/csv";

function lines(document: string) {
    return document.replace(/^\uFEFF/, "").split("\r\n");
}

test("the encoder writes a byte order mark, CRLF rows, quoted text and empty missing values", () => {
    const document = csvDocument(
        ["a", "b", "c"],
        [
            [1, "x", null],
            [2.5, 'say "hi", then', ""],
        ],
    );
    expect(document.startsWith("\uFEFF")).toBe(true);
    expect(lines(document)).toEqual([
        '"a","b","c"',
        '1,"x",',
        '2.5,"say ""hi"", then",""',
        "",
    ]);
});

test("the encoder neutralises leading formula characters but keeps plain signed numbers", () => {
    expect(
        lines(csvDocument(["a"], [["=1+1"], [" @x"], ["-2"], ["\t+3"]])),
    ).toEqual([`"a"`, `"'=1+1"`, `"' @x"`, `"-2"`, `"'\t+3"`, ""]);
});

test("a non-finite number is written as a missing value", () => {
    expect(lines(csvDocument(["a", "b"], [[Number.NaN, Infinity]]))[1]).toBe(
        ",",
    );
});

test("an empty row list gives the header only", () => {
    expect(lines(csvDocument(["timezone"], []))).toEqual(['"timezone"', ""]);
});
