"use client";
import type { CSSProperties } from "react";
import { brandProperties } from "@/styles/tokens";
import "./globals.css";
import en from "../../messages/en/shell.json";
import tr from "../../messages/tr/shell.json";

export default function GlobalError({
    retry,
}: {
    error: unknown;
    retry: () => void;
}) {
    return (
        <html lang="en" style={brandProperties as CSSProperties}>
            <body>
                <title>{`${en.AppError.title} | Pyracast`}</title>
                <main className="auth-page">
                    <section className="panel narrow">
                        <h1>{en.AppError.title}</h1>
                        <p>{en.AppError.description}</p>
                        <p lang="tr">
                            <strong>{tr.AppError.title}.</strong>{" "}
                            {tr.AppError.description}
                        </p>
                        <button
                            className="button"
                            type="button"
                            onClick={() => retry()}
                        >
                            {en.AppError.retry} /{" "}
                            <span lang="tr">{tr.AppError.retry}</span>
                        </button>
                    </section>
                </main>
            </body>
        </html>
    );
}
