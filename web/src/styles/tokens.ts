// Colours and spacing for places that cannot read CSS custom properties: SVG presentation attributes, generated images and the QR code.
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
    "--amber-61": "#fef3e1",
    "--amber-69": "#f9f0e2",
    "--amber-231": "#f3ce95",
    "--amber-290": "#ffc46b",
    "--amber-300": "#dabb8b",
    "--amber-329-a0": "#f7bb5f00",
    "--amber-329-a20": "#f7bb5f33",
    "--amber-600": "#a47328",
    "--amber-690": "#9e5f00",
    "--amber-755": "#7d4b00",
    "--cream": brand.cream,
    "--cream-35": "#f9f8f3",
} as const;

export const illustrationColours = {
    forecast: "#28664e",
    grid: "#dedfd3",
    axis: "#c4ccbd",
    production: brandProperties["--amber-600"],
    halo: "#fbfbf5",
};

export const socialImageColours = {
    background: brand.forest,
    text: brand.cream,
    accent: brandProperties["--amber-290"],
    muted: "#c7d9cc",
};

export const socialImageSpacing = { page: "70px 80px", lines: 26 };
