import { createServer } from "node:net";

// The operating system releases the listener however this process ends, so the lock cannot go stale.
export function holdCoverageLock(port) {
    return new Promise((resolve, reject) => {
        const listener = createServer((connection) => connection.destroy());
        listener.once("error", (error) => {
            if (error.code === "EADDRINUSE") resolve(undefined);
            else reject(error);
        });
        listener.listen({ host: "127.0.0.1", port, exclusive: true }, () => {
            listener.unref();
            resolve(listener.address().port);
        });
    });
}
