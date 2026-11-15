import { createSocialImage } from "@/components/public-site/social-image";

export const alt = "Pyracast — güneş enerjisi üretim tahmini";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function Image() {
    return createSocialImage("tr");
}
