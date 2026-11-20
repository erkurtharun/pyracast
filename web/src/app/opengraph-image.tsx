import { createSocialImage } from "@/features/public-site/social-image";

export const alt = "Pyracast — solar production forecasts";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function Image() {
    return createSocialImage("en");
}
