import type { Metadata } from "next";
import { NotFoundLink } from "@/features/public-site/not-found-link";
import { SiteShell } from "@/features/public-site/site-shell";
import styles from "@/features/public-site/public-site.module.css";
import { publicPath } from "@/lib/public-metadata";
import { routes } from "@/lib/routes";

export const metadata: Metadata = {
    title: { absolute: "Page not found | Pyracast" },
};

// The root locale here is the visitor's negotiated language, never the requested path, so both languages show.
export default function NotFound() {
    return (
        <SiteShell language="en">
            <main id="main-content" className={styles.legalMain} tabIndex={-1}>
                <header className={styles.legalHeader} lang="en">
                    <p className={styles.eyebrow}>404</p>
                    <h1>Page not found</h1>
                    <p className={styles.legalIntro}>
                        The address may be mistyped or the page may have moved.{" "}
                        <NotFoundLink
                            home={{
                                href: publicPath("en", "home"),
                                label: "Go to the homepage",
                            }}
                            app={{
                                href: routes.entry(),
                                label: "Go to the app",
                            }}
                        />
                    </p>
                </header>
                <section className={styles.legalHeader} lang="tr">
                    <h2>Sayfa bulunamadı</h2>
                    <p className={styles.legalIntro}>
                        Adres yanlış yazılmış ya da sayfa taşınmış olabilir.{" "}
                        <NotFoundLink
                            home={{
                                href: publicPath("tr", "home"),
                                label: "Ana sayfaya git",
                            }}
                            app={{
                                href: routes.entry(),
                                label: "Uygulamaya git",
                            }}
                        />
                    </p>
                </section>
            </main>
        </SiteShell>
    );
}
