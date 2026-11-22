import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { PNG } from "pngjs";

const web = path.resolve(__dirname, "..");

function publicFile(url: string) {
    return readFileSync(path.join(web, "public", url));
}

function token(name: string) {
    const tokens = readFileSync(
        path.join(web, "src/styles/tokens.css"),
        "utf8",
    );
    return tokens.match(new RegExp(`--${name}: (#[0-9a-f]{6});`))?.[1];
}

function image(url: string) {
    return PNG.sync.read(publicFile(url));
}

function pixel(png: PNG, x: number, y: number) {
    const offset = (y * png.width + x) * 4;
    return [...png.data.subarray(offset, offset + 4)];
}

function rgba(hex: string) {
    return [1, 3, 5].map((start) =>
        Number.parseInt(hex.slice(start, start + 2), 16),
    );
}

type Icon = { src: string; sizes: string; type: string; purpose: string };
const manifest = JSON.parse(
    publicFile("app/manifest.webmanifest").toString("utf8"),
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
        icons: [
            {
                src: "/app/icon-192.png",
                sizes: "192x192",
                type: "image/png",
                purpose: "any",
            },
            {
                src: "/app/icon-512.png",
                sizes: "512x512",
                type: "image/png",
                purpose: "any",
            },
            {
                src: "/app/icon-512-maskable.png",
                sizes: "512x512",
                type: "image/png",
                purpose: "maskable",
            },
        ],
    });
});

test("every manifest icon is a PNG of its declared size", () => {
    for (const icon of manifest.icons) {
        expect(icon.type, icon.src).toBe("image/png");
        const png = image(icon.src);
        expect(`${png.width}x${png.height}`, icon.src).toBe(icon.sizes);
    }
});

test("the maskable icon fills its square and keeps the mark inside the safe zone", () => {
    const png = image("/app/icon-512-maskable.png");
    const background = [...rgba(token("forest")!), 255];
    const centre = png.width / 2;
    const safe = png.width * 0.4;
    const outside: string[] = [];
    for (let y = 0; y < png.height; y++)
        for (let x = 0; x < png.width; x++) {
            if (Math.hypot(x + 0.5 - centre, y + 0.5 - centre) <= safe)
                continue;
            if (pixel(png, x, y).join() !== background.join())
                outside.push(`${x},${y}`);
        }
    expect(outside).toEqual([]);
});

test("the touch icon is a fully opaque 180 px square", () => {
    const png = image("/app/apple-touch-icon.png");
    expect(`${png.width}x${png.height}`).toBe("180x180");
    const translucent = [];
    for (let index = 3; index < png.data.length; index += 4)
        if (png.data[index] !== 255) translucent.push(index);
    expect(translucent).toEqual([]);
    expect(pixel(png, 0, 0)).toEqual([...rgba(token("forest")!), 255]);
});
