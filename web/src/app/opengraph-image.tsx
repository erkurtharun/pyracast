import { ImageResponse } from "next/og";

export const alt = "Pyracast — solar production forecasts";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function Image() {
    return new ImageResponse(
        <div
            style={{
                display: "flex",
                flexDirection: "column",
                justifyContent: "space-between",
                width: "100%",
                height: "100%",
                padding: "70px 80px",
                background: "#143c32",
                color: "#f6f7f3",
            }}
        >
            <div
                style={{ display: "flex", alignItems: "center", fontSize: 48 }}
            >
                pyracast<span style={{ color: "#efc778" }}>.</span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 26 }}>
                <div style={{ fontSize: 76, maxWidth: 900, lineHeight: 1.1 }}>
                    A clearer view of tomorrow’s solar production.
                </div>
                <div style={{ color: "#c7d9cc", fontSize: 28 }}>
                    Forecasts · Reported production · One workspace
                </div>
            </div>
            <div style={{ color: "#efc778", fontSize: 24 }}>pyracast.app</div>
        </div>,
        size,
    );
}
