import { NotFoundPage } from "@/features/public-site/not-found-page";

// Inside the /app layout, so its document title stays mounted; the root boundary would unmount it.
export default function AppNotFound() {
    return <NotFoundPage inApp />;
}
