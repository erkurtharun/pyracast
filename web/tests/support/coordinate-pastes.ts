// Text pasted into Latitude, in the workspace and in the console alike: a pair fills Latitude and
// Longitude as pasted; any other text stays in Latitude, where the single-value rule takes it as
// one number or refuses it.
export type CoordinatePaste =
    | { text: string; pair: readonly [string, string] }
    | { text: string; single: "number" | "refused" };

export const coordinatePastes: readonly CoordinatePaste[] = [
    { text: "41.0082,28.9784", pair: ["41.0082", "28.9784"] },
    { text: "41.0082, 28.9784", pair: ["41.0082", "28.9784"] },
    { text: "41,0082; 28,9784", pair: ["41,0082", "28,9784"] },
    { text: "41.0082 28.9784", pair: ["41.0082", "28.9784"] },
    { text: "41,0082, 28,9784", pair: ["41,0082", "28,9784"] },
    { text: "41,5 28,9", pair: ["41,5", "28,9"] },
    { text: " -41.5 ; 28.9 ", pair: ["-41.5", "28.9"] },
    { text: "41.5\t-28.9", pair: ["41.5", "-28.9"] },
    { text: "-41.5,-28", pair: ["-41.5", "-28"] },
    { text: "-41,-28", pair: ["-41", "-28"] },
    { text: "41 , 29", pair: ["41", "29"] },
    { text: "-41; -29", pair: ["-41", "-29"] },
    { text: "-90, 180", pair: ["-90", "180"] },
    { text: "1.234,5", single: "refused" },
    { text: "-1.234,5", single: "refused" },
    { text: "1,234.5", single: "refused" },
    { text: "1,123.5", single: "refused" },
    { text: "12.345,67", single: "refused" },
    { text: "41,0082.5", single: "refused" },
    { text: "41,0082", single: "number" },
    { text: "-36,9", single: "number" },
    { text: "41", single: "number" },
    { text: "41,,29", single: "refused" },
    { text: "41 29 30", single: "refused" },
    { text: "41. 29", single: "refused" },
    { text: "NaN,29", single: "refused" },
    { text: "41.5, 0082", single: "refused" },
    { text: "91, 29", single: "refused" },
    { text: "41, 181", single: "refused" },
    { text: "1 234", single: "refused" },
    { text: "41,0082,28,9784", single: "refused" },
];
