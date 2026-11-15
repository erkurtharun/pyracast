import type { ReactNode } from "react";
import { publicPath, type PublicPage } from "@/lib/public-metadata";
import { homeCopy, type PublicLanguage } from "./home-copy";
import styles from "./public-site.module.css";

export function SiteShell({
    language,
    page = "home",
    children,
}: {
    language: PublicLanguage;
    page?: PublicPage;
    children: ReactNode;
}) {
    const copy = homeCopy[language];
    const other = language === "en" ? "tr" : "en";
    const home = publicPath(language, "home");
    return (
        <div className={styles.site}>
            <a className={styles.skipLink} href="#main-content">
                {copy.skip}
            </a>
            <header className={styles.header}>
                <div className={styles.headerInner}>
                    <a
                        className={styles.brand}
                        href={home}
                        aria-label="Pyracast"
                    >
                        <svg
                            width="28"
                            height="28"
                            viewBox="0 0 28 28"
                            fill="none"
                            aria-hidden="true"
                        >
                            <circle
                                cx="14"
                                cy="14"
                                r="5"
                                stroke="currentColor"
                                strokeWidth="2"
                            />
                            <path
                                d="M14 2v4m0 16v4M2 14h4m16 0h4M5.5 5.5l3 3m11 11 3 3m0-17-3 3m-11 11-3 3"
                                stroke="currentColor"
                                strokeWidth="2"
                                strokeLinecap="round"
                            />
                        </svg>
                        <span>
                            pyracast<span className={styles.brandDot}>.</span>
                        </span>
                    </a>
                    <nav
                        className={styles.desktopNav}
                        aria-label={
                            language === "en" ? "Main navigation" : "Ana menü"
                        }
                    >
                        <a href={`${home}#product`}>{copy.nav}</a>
                        <a href={`${home}#how-it-works`}>{copy.stepsNav}</a>
                        <a href={`${home}#questions`}>{copy.faqNav}</a>
                    </nav>
                    <div className={styles.headerActions}>
                        <a
                            className={styles.language}
                            href={publicPath(other, page)}
                            hrefLang={other}
                            lang={other}
                        >
                            {other === "tr" ? "Türkçe" : "English"}
                        </a>
                        <a
                            className={styles.login}
                            href={`/app/?language=${language}`}
                        >
                            {copy.login}
                            <span aria-hidden="true"> ↗</span>
                        </a>
                    </div>
                </div>
            </header>
            {children}
            <footer className={styles.footer}>
                <div className={styles.footerInner}>
                    <div>
                        <a className={styles.footerBrand} href={home}>
                            pyracast<span className={styles.brandDot}>.</span>
                        </a>
                        <p>{copy.footer}</p>
                    </div>
                    <nav
                        aria-label={
                            language === "en"
                                ? "Legal and contact"
                                : "Yasal bilgiler ve iletişim"
                        }
                    >
                        <a href={publicPath(language, "privacy")}>
                            {copy.privacy}
                        </a>
                        <a href={publicPath(language, "terms")}>{copy.terms}</a>
                        <a href="mailto:info@pyracast.app">info@pyracast.app</a>
                    </nav>
                </div>
            </footer>
        </div>
    );
}
