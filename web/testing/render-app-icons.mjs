import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "@playwright/test";

const web = path.resolve(import.meta.dirname, "..");
const output = path.join(web, "public", "app");
const icons = [
    { file: "icon-192.png", size: 192, shape: "rounded", markScale: 1 },
    { file: "icon-512.png", size: 512, shape: "rounded", markScale: 1 },
    { file: "icon-512-maskable.png", size: 512, shape: "full", markScale: 0.8 },
    { file: "apple-touch-icon.png", size: 180, shape: "full", markScale: 1 },
];

const source = await readFile(path.join(web, "src", "app", "icon.svg"), "utf8");
await mkdir(output, { recursive: true });
const browser = await chromium.launch();
try {
    for (const icon of icons) {
        const page = await browser.newPage({
            viewport: { width: icon.size, height: icon.size },
            deviceScaleFactor: 1,
        });
        await page.setContent(
            `<!doctype html><body style="margin:0">${source}</body>`,
        );
        await page
            .locator("svg")
            .evaluate((svg, { size, shape, markScale }) => {
                svg.setAttribute("width", String(size));
                svg.setAttribute("height", String(size));
                svg.style.display = "block";
                if (shape === "full")
                    svg.querySelector("rect").removeAttribute("rx");
                const box = svg.viewBox.baseVal;
                const x = box.x + box.width / 2;
                const y = box.y + box.height / 2;
                svg.querySelector("g").setAttribute(
                    "transform",
                    `translate(${x} ${y}) scale(${markScale}) translate(${-x} ${-y})`,
                );
            }, icon);
        await writeFile(
            path.join(output, icon.file),
            await page.locator("svg").screenshot({
                omitBackground: icon.shape === "rounded",
            }),
        );
        await page.close();
    }
} finally {
    await browser.close();
}
