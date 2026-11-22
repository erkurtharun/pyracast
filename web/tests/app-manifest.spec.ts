import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { PNG } from "pngjs";

const web = path.resolve(__dirname, "..");
const manifestAddress = new URL("http://app.test/app/manifest.webmanifest");

function publicFile(address: string) {
    return path.join(web, "public", new URL(address, manifestAddress).pathname);
}

function token(name: string) {
    const tokens = readFileSync(
        path.join(web, "src/styles/tokens.css"),
        "utf8",
    );
    return tokens.match(new RegExp(`--${name}: (#[0-9a-f]{6});`))?.[1];
}

function image(address: string) {
    const file = readFileSync(publicFile(address));
    const header = PNG.sync.read(file, { checkCRC: true });
    return { header, signature: file.subarray(1, 4).toString("ascii") };
}

function pixel(png: PNG, x: number, y: number) {
    const offset = (y * png.width + x) * 4;
    return [...png.data.subarray(offset, offset + 4)].join();
}

function translucentPixels(png: PNG) {
    const translucent = [];
    for (let index = 3; index < png.data.length; index += 4)
        if (png.data[index] !== 255) translucent.push(index);
    return translucent;
}

function edgePixels(png: PNG) {
    const edges = [];
    for (let i = 0; i < png.width; i++)
        edges.push([i, 0], [i, png.height - 1], [0, i], [png.width - 1, i]);
    return edges.map(([x, y]) => pixel(png, x, y));
}

function markOutsideSafeZone(png: PNG) {
    const background = pixel(png, 0, 0);
    const centre = png.width / 2;
    const safe = png.width * 0.4;
    const outside: string[] = [];
    for (let y = 0; y < png.height; y++)
        for (let x = 0; x < png.width; x++) {
            if (Math.hypot(x + 0.5 - centre, y + 0.5 - centre) <= safe)
                continue;
            if (pixel(png, x, y) !== background) outside.push(`${x},${y}`);
        }
    return outside;
}

type Icon = { src: string; sizes: string; type: string; purpose: string };
const manifest = JSON.parse(
    readFileSync(publicFile("/app/manifest.webmanifest"), "utf8"),
) as { icons: Icon[] };

test("the app manifest declares one fixed identity under /app/ and nothing else", () => {
    expect(manifest).toEqual({
        id: "/app/",
        name: "Pyracast",
        short_name: "Pyracast",
        start_url: "/app/",
        scope: "/app/",
        display: "standalone",
        theme_color: token("forest"),
        background_color: token("cream"),
        icons: [192, 512].flatMap((size) =>
            ["any", "maskable"].map((purpose) => ({
                src: `icons/${purpose}-${size}.png`,
                sizes: `${size}x${size}`,
                purpose,
                type: "image/png",
            })),
        ),
    });
});

test("every manifest icon is a PNG of its declared size", () => {
    for (const icon of manifest.icons) {
        expect(existsSync(publicFile(icon.src)), icon.src).toBe(true);
        const { header, signature } = image(icon.src);
        expect(icon.type, icon.src).toBe("image/png");
        expect(signature, icon.src).toBe("PNG");
        expect(`${header.width}x${header.height}`, icon.src).toBe(icon.sizes);
    }
});

test("the maskable icons are opaque, full bleed and keep the mark inside the safe zone", () => {
    for (const icon of manifest.icons.filter(
        ({ purpose }) => purpose === "maskable",
    )) {
        const { header } = image(icon.src);
        expect(translucentPixels(header), icon.src).toEqual([]);
        expect(new Set(edgePixels(header)), icon.src).toEqual(
            new Set([pixel(header, 0, 0)]),
        );
        expect(markOutsideSafeZone(header), icon.src).toEqual([]);
    }
});

test("the touch icon is a fully opaque 180 px PNG", () => {
    const { header, signature } = image("/app/icons/apple-touch-icon.png");
    expect(signature).toBe("PNG");
    expect(`${header.width}x${header.height}`).toBe("180x180");
    expect(translucentPixels(header)).toEqual([]);
});
