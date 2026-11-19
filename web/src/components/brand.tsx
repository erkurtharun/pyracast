export function Brand() {
    return (
        <div className="brand">
            <svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true">
                <circle
                    cx="14"
                    cy="14"
                    r="5"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                />
                <path
                    d="M14 2v4m0 16v4M2 14h4m16 0h4M5.5 5.5l3 3m11 11 3 3m0-17-3 3m-11 11-3 3"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                />
            </svg>{" "}
            pyracast
            <span className="brand-dot">.</span>
        </div>
    );
}
