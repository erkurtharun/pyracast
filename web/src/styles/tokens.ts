export const qrColours = { background: "#ffffff", foreground: "#000000" };

export const brand = {
    forest: "#09383a",
    amber: "#ffb545",
    cream: "#f4f2e8",
};

export const brandProperties = {
    "--forest": brand.forest,
    "--forest-822": "#144547",
    "--amber": brand.amber,
    "--amber-65": "#fef2df",
    "--amber-69": "#f9f0e2",
    "--amber-220": "#eecfa0",
    "--amber-300": "#dabb8b",
    "--amber-329-a0": "#f7bb5f00",
    "--amber-329-a20": "#f7bb5f33",
    "--amber-600": "#a47328",
    "--amber-684": "#a16100",
    "--amber-755": "#7d4b00",
    "--cream": brand.cream,
    "--cream-39": "#f9f7f2",
    "--cream-45": "#f8f6ef",
    "--teal-65": "#e9f3f4",
    "--teal-708": "#207175",
    "--teal-729": "#236467",
    "--teal-753": "#11696d",
    "--teal-802": "#184b4d",
    "--teal-824": "#0c4b4e",
    "--teal-841-a3": "#153b3c08",
    "--teal-841-a4": "#153b3c0a",
    "--teal-841-a5": "#153b3c0d",
} as const;

export const imageShades = { "--amber-296": "#fec369" } as const;

export const illustrationColours = {
    grid: "#dedfd3",
    axis: "#c4ccbd",
    production: brandProperties["--amber-600"],
    halo: "#fbfbf5",
};

export const socialImageColours = {
    background: brand.forest,
    text: brand.cream,
    accent: imageShades["--amber-296"],
    muted: "#c7d9cc",
};

export const socialImageSpacing = { page: "70px 80px", lines: 26 };
