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
    return PNG.sync.read(readFileSync(publicFile(address)), { checkCRC: true });
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
    const last = png.width - 1;
    const edges = [];
    for (let i = 0; i <= last; i++)
        edges.push(
            pixel(png, i, 0),
            pixel(png, i, last),
            pixel(png, 0, i),
            pixel(png, last, i),
        );
    return edges;
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
        icons: ["any", "maskable"].flatMap((purpose) =>
            [192, 512].map((size) => ({
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
        const header = image(icon.src);
        expect(icon.type, icon.src).toBe("image/png");
        expect(`${header.width}x${header.height}`, icon.src).toBe(icon.sizes);
    }
});

test("the maskable icons are opaque, full bleed and keep the mark inside the safe zone", () => {
    for (const icon of manifest.icons.filter(
        ({ purpose }) => purpose === "maskable",
    )) {
        const header = image(icon.src);
        expect(translucentPixels(header), icon.src).toEqual([]);
        expect(new Set(edgePixels(header)), icon.src).toEqual(
            new Set([pixel(header, 0, 0)]),
        );
        expect(markOutsideSafeZone(header), icon.src).toEqual([]);
    }
});

test("the touch icon is a fully opaque 180 px PNG", () => {
    const header = image("/app/icons/apple-touch-icon.png");
    expect(`${header.width}x${header.height}`).toBe("180x180");
    expect(translucentPixels(header)).toEqual([]);
});
