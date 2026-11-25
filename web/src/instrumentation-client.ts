import { reloadGuardedTraversal } from "@/lib/leave-guard";

// Added before hydration, so it runs before the router's listener and can stop a guarded traversal.
window.addEventListener("popstate", reloadGuardedTraversal);
