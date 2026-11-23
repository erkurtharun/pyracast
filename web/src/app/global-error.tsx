"use client";
import "./globals.css";
import { reloadPage } from "@/lib/on-demand";
import { useScreenTitle } from "@/ui/page-title";
import en from "../../messages/en/shell.json";
import tr from "../../messages/tr/shell.json";

export default function GlobalError({
    retry,
}: {
    error: unknown;
    retry: () => void;
}) {
    const title = useScreenTitle(en.AppError.title);
    return (
        <html lang="en">
            <body>
                <title>{title}</title>
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
                        <button
                            className="text-button"
                            type="button"
                            onClick={reloadPage}
                        >
                            {en.AppError.reload} /{" "}
                            <span lang="tr">{tr.AppError.reload}</span>
                        </button>
                    </section>
                </main>
            </body>
        </html>
    );
}
