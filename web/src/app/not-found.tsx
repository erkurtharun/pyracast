import type { Metadata } from "next";
import { SiteShell } from "@/components/public-site/site-shell";
import styles from "@/components/public-site/public-site.module.css";
import { publicPath } from "@/lib/public-metadata";

export const metadata: Metadata = {
    title: { absolute: "Page not found | Pyracast" },
};

// The root locale here is the cookie or English, never the requested path, so both languages show.
export default function NotFound() {
    return (
        <SiteShell language="en">
            <main id="main-content" className={styles.legalMain} tabIndex={-1}>
                <header className={styles.legalHeader} lang="en">
                    <p className={styles.eyebrow}>404</p>
                    <h1>Page not found</h1>
                    <p className={styles.legalIntro}>
                        The address may be mistyped or the page may have moved.{" "}
                        <a href={publicPath("en", "home")}>
                            Go to the homepage
                        </a>
                    </p>
                </header>
                <section className={styles.legalHeader} lang="tr">
                    <h2>Sayfa bulunamadı</h2>
                    <p className={styles.legalIntro}>
                        Adres yanlış yazılmış ya da sayfa taşınmış olabilir.{" "}
                        <a href={publicPath("tr", "home")}>Ana sayfaya git</a>
                    </p>
                </section>
            </main>
        </SiteShell>
    );
}
