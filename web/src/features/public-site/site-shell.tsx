import type { ReactNode } from "react";
import { publicPath, type PublicPage } from "@/lib/public-metadata";
import { homeCopy, type PublicLanguage } from "./home-copy";
import { routes } from "@/lib/routes";
import { Brand } from "@/ui/brand";
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
                    <a className={styles.brand} href={home}>
                        <Brand surface="light" />
                    </a>
                    <nav
                        className={styles.desktopNav}
                        aria-label={
                            language === "en" ? "Main navigation" : "Ana menü"
                        }
                    >
                        <a href={`${home}#product`}>{copy.nav}</a>
                        <a href={`${home}#getting-started`}>{copy.startNav}</a>
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
                            href={routes.signIn(language)}
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
                            <Brand surface="light" />
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
