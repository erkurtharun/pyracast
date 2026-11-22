import { publicPath } from "@/lib/public-metadata";
import { routes } from "@/lib/routes";
import { SiteShell } from "./site-shell";
import styles from "./public-site.module.css";

export function NotFoundPage({ inApp = false }: { inApp?: boolean }) {
    return (
        <SiteShell language="en">
            <main id="main-content" className={styles.legalMain} tabIndex={-1}>
                <header className={styles.legalHeader} lang="en">
                    <p className={styles.eyebrow}>404</p>
                    <h1>Page not found</h1>
                    <p className={styles.legalIntro}>
                        The address may be mistyped or the page may have moved.{" "}
                        {inApp ? (
                            <a href={routes.entry()}>Go to the app</a>
                        ) : (
                            <a href={publicPath("en", "home")}>
                                Go to the homepage
                            </a>
                        )}
                    </p>
                </header>
                <section className={styles.legalHeader} lang="tr">
                    <h2>Sayfa bulunamadı</h2>
                    <p className={styles.legalIntro}>
                        Adres yanlış yazılmış ya da sayfa taşınmış olabilir.{" "}
                        {inApp ? (
                            <a href={routes.entry()}>Uygulamaya git</a>
                        ) : (
                            <a href={publicPath("tr", "home")}>
                                Ana sayfaya git
                            </a>
                        )}
                    </p>
                </section>
            </main>
        </SiteShell>
    );
}
