# Keep figures on a device: the switch, the refresh and the saved pages

A person turns on "Keep figures on this device" in the workspace sidebar, or answers the offer an installed window puts
once. From then on the browser holds the forecast and production figures of up to eight of the person's plants, and
the application reads each plant's days again when they are due. When the server does not answer a page load under
`/app/`, the service worker shows a page built from those figures. Sign-out, another person's sign-in, the switch, a
removal on a saved page and 90 days without a sign-in remove them. The server keeps nothing for any of this.

## Entry points

- `web/src/features/saved/keep-figures.tsx`: `KeepFigures`, a `details` beside "Install app" in the sidebar
  (`web/src/features/workspace/workspace-shell.tsx`), and `KeepFiguresOffer`, a notice above the workspace. Texts are
  the `KeepFigures` namespace of `web/messages/{en,tr}/core.json`.
- `web/src/features/saved/saved-figures.ts`: the plant and workspace on screen (`useShownPlant` in
  `web/src/features/plant/plant-workspace.tsx`, `useShownWorkspace` in the switch), the mount of the refresh
  (`useSavedFiguresRefresh` in `web/src/features/workspace/workspace-shell.tsx`, so only the workspace screens refresh)
  and the registration check. The mount reads the state again at every committed change of the copy, at every change
  of the flag or a removal mark in another window and when the window becomes visible.
- `web/src/features/saved/refresh.ts`: the refresh, loaded through `import()` only while it runs.
- `web/src/lib/saved/feature.ts`: the constant `SAVED_FIGURES`, in the first load so a withdrawn build loads neither the
  refresh nor the switch's store reads.
- `web/src/lib/saved/store.ts`: the only IndexedDB code of the application, loaded through `import()`.
  `web/src/lib/session-client.ts` runs every store operation through one queue per document, one Web Lock per browser
  (`pyracast.saved-copy`) and one deadline of 2 s per operation, and keeps the flag, the removal marks and the change
  signal.
- `web/src/lib/api/query.ts` `readListOnce`: the workspace list and the site list, read through the query cache under
  the screens' key with a `staleTime` of 30 s.
- The days read: `GET /api/tenants/{tenant}/regions/{site}/plants/{plant}/days/` (`PlantDaysView` in
  `src/pyracast/plants/api.py`, the answer from `src/pyracast/plants/plant_days.py`).
- `web/public/app/sw.js`: the reader and the saved pages (`docs/flows/installed-app.md`).

## Sequence

```mermaid
sequenceDiagram
    participant P as Person
    participant A as Application
    participant L as Web Locks
    participant I as IndexedDB pyracast-saved
    participant S as API
    participant W as Worker
    P->>A: Turn on
    A->>A: localStorage pyracast.saved = new value, read back
    A->>L: pyracast.saved-copy
    A->>I: if the session, its revision, the sign-out state and the flag are unchanged: create, write holder
    A->>A: pyracast.savedChange = new value after the commit; navigator.storage.persist()
    A->>L: pyracast-saved-refresh (waits for another window's run)
    A->>S: GET /api/tenants/ (cache, 30 s)
    A->>I: remove the workspaces it leaves out
    A->>S: the site list of the workspace on screen (cache, 30 s)
    A->>I: settle plants (remove, rename, register up to 8)
    loop each due plant, one at a time, while visible
        A->>I: mark attempt
        A->>S: GET .../days/
        S-->>A: days of the window
        A->>I: replace the plant's days if the attempt is still the latest
    end
    Note over W: later, a page load gets no answer
    W->>I: read holder, plants, days
    W-->>P: 503 saved page or notice
```

## What the device holds

| Storage                                          | Written by                                                        | Content                                                                                                                                                                                                                                                                                                                                        |
| ------------------------------------------------ | ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pyracast.saved` (`localStorage`)                | Turn on, a deletion                                               | The flag: a new random value at every turn on, which must read back before the database is created; a deletion forgets it only while it still holds the value read when the deletion was asked for                                                                                                                                             |
| `pyracast.savedChange` (`localStorage`)          | A committed turn on, removal from the switch or pause at sign-out | A random value; other windows read the switch, the cover and the refresh's state again                                                                                                                                                                                                                                                         |
| `pyracast.savedRemoval.<token>` (`localStorage`) | A sign-out whose removal did not finish in 2 s                    | One key per departing session, `{ removal, user, session }`; the next start, or the next adoption, judges every key inside the transaction that acts on the holder and deletes the keys it judged; with no flag, or no database, the keys are dropped                                                                                          |
| `pyracast.savedSignOut` (`localStorage`)         | The start of every sign-out, in any window                        | A random value; a window whose session was adopted before it changed turns on nothing and sends no refresh request until it adopts another session                                                                                                                                                                                             |
| `pyracast.sessionRevision` (`localStorage`)      | Every change of the observed session tag                          | A random value; a session read is used only when tag and revision are what they were when it started                                                                                                                                                                                                                                           |
| `pyracast.savedOffer` (`localStorage`)           | "Turn on" or "Not now" in the offer                               | Set once; the offer is not put again, and leaves every other open window                                                                                                                                                                                                                                                                       |
| `pyracast-saved` (IndexedDB, version 1)          | `store.ts`, the worker                                            | `holder` under the key `"holder"`: `{ user, language, session, active, confirmedAt, removal, retryAt }`; `plants` keyed `[tenant, site, plant]`: `{ name, workspace, openedAt, attemptedAt, refreshedAt }`; `days` keyed `[tenant, site, plant, kind, date]`: the day object of the days read unchanged. Times are `Date.now()` values or null |

## Rules and where they are implemented

| Rule (PLAN section 2)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Implemented in                                                                                                                                                                       |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Offline reading: only the worker's answer to a page load the server did not answer; figures of up to 8 plants, forecast 7 days back to 14 ahead, production 30 days back, as the days read answers                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | `web/public/app/sw.js` (reader, builder); window in `src/pyracast/plants/plant_days.py`; bound of 8 in `store.ts` `openPlant`, `settle`                                              |
| Saved pages: no script, links built from integer ids, notice CSP, removal on a saved page with the token                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | `web/public/app/sw.js`                                                                                                                                                               |
| The days read: one read per plant, scope `days` at 120 an hour, one read per API process at a time, refused day `calculation_out_of_range`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | `src/pyracast/plants/api.py` `PlantDaysView`, `src/pyracast/plants/plant_days.py`, `src/pyracast/settings.py`, `src/pyracast/api/permissions.py`                                     |
| The copy: created only by turning on; at most 8 plants (one never opened leaves first, then the least recently opened), 22 forecast and 30 production days, 52 rows a day; one transaction replaces a plant's days                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | `store.ts` `turnOn`, `openPlant`, `settle`, `writeDays`                                                                                                                              |
| Refresh: only inside the workspace screens; at the mount, turn on, a plant opened, `online`, visible, every 5 min while visible, a changed production answer of the plant on screen; a start during a run runs once more after it; due when never read, the plant on screen after 15 min or after a production change, any other after 60 min, never within 5 min of the last attempt; one run per browser, a start in another window waits for it and then judges by its own screen; the holder is checked before any list read, and the window's visibility, the holder and the session before each request and before an attempt is marked; an answered workspace list removes the workspaces it leaves out at once; a 403 removes the workspace, a 404 the plant, a 429 stops days reads for the time it names and at least 5 min, any other failure ends the run | `web/src/features/saved/refresh.ts`, `store.ts` `markAttempt`, `writeDays`, `setRetryAt`, `removeWorkspace`, `removePlant`                                                           |
| Who may read the copy: the switch and its offer; 90 days after the last signed-in start; sign-out removes the figures before its request; another user's sign-in deletes the database; turning off deletes it                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | `keep-figures.tsx`; `store.ts` `adopt`, `removeFigures`, `removeCopy`; `session-client.ts` `signOut`, `turnOnSavedFigures`, `removeSavedFiguresHere`; the worker's expiry in `sw.js` |

## Authorization

- The days read is a member read of any role, checked as the forecast read checks a plant; the device keeps only
  figures the person could download as CSV.
- A plant or day is written only while the holder is active and its user and session tag equal the document's, checked
  in the transaction that writes; an answer whose attempt was overtaken is dropped there too.
- The switch is offered only where the worker is registered for `/app/`, `SAVED_FIGURES` is on and IndexedDB answers
  an open; without a registration the copy is not refreshed either, since only the worker reads it.
- A Turn on that waited for the lock changes nothing when, inside its transaction, the session, the observed revision,
  the sign-out state or the flag it wrote is no longer the one it started with, or any window began a sign-out since
  the session was adopted, also one that found no database to clear.
- With `SAVED_FIGURES` off no refresh starts, the switch, its offer and "Show last loaded figures" are not shown and
  their store reads are not loaded, whatever the copy holds and whether its deletion at start succeeded.

## Failure paths

| Case                                                    | Behaviour                                                                                                                                                                                                                                                                                       |
| ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The database cannot be opened, or is of a newer format  | No switch; a signed-in start that has a flag shows "The figures kept on this device could not be checked." with Try again                                                                                                                                                                       |
| A days read answers 403 or 404                          | The workspace's plants, or the plant, are removed and the run goes on                                                                                                                                                                                                                           |
| A days read answers 429                                 | `retryAt` is set to the time it names, at least 5 min ahead; no days read is sent before                                                                                                                                                                                                        |
| A days read answers 401, 500 or gets no answer          | The run ends; the held days stay                                                                                                                                                                                                                                                                |
| The site list fails after the workspace list answered   | The workspaces the list left out are already removed; on 403 or 404 the workspace on screen leaves the device and the run goes on with the other plants, otherwise the run ends                                                                                                                 |
| The window is hidden                                    | No run starts and a running one sends no further request                                                                                                                                                                                                                                        |
| A removal mark whose copy is gone                       | Dropped at the next start                                                                                                                                                                                                                                                                       |
| Another window holds the lock                           | The operation waits within its 2 s deadline; a refresh start waits for the other window's run and then runs                                                                                                                                                                                     |
| A session read answers while the store is busy past 2 s | The same owner with no other session observed since its adoption: the screen and the refresh go on, and the hand-over (removal marks, language, the 90 days) runs behind it and is retried at the next read; another owner or revision: "The figures kept on this device could not be checked." |

## Cost of the days read

Measured in K70: about 8 queries per forecast day without a calibration, 13 with one usable calibration and 15 with a
usable calibration that carries provider evidence; no query per production day. The read stays under 25 single-day
forecast reads, also at ten times the forecast history (about 14 to 15 on a development machine).

## Tests

- `web/tests/keep-figures.spec.ts`: the switch and the offer, a refused flag, a Turn on overtaken by a session change
  or by another window's sign-out, a session read again while the store is busy, committed changes and the offer in other windows, the refresh's cadence, visibility, a window hidden while its run judges the plants, a start during a run or in another window's run, the 429
  pause, the 403, 404 and 500 answers, a workspace left out of the list, a plant opened, a production change, two
  windows, the invitation page, the cached site list, the store's bounds and write guards, the privacy notice.
- `web/tests/saved-figures-withdrawn.spec.ts`: runs only when `feature.ts` sets `SAVED_FIGURES` to `false`; no switch,
  offer, cover button, refresh code or read over a copy whose deletion failed.
- `web/tests/saved-copy.spec.ts`: adoption, every removal, the marks, the observed revision, the notice.
- `web/tests/saved-pages.spec.ts` and `web/production-tests/saved-pages.spec.ts`: the worker's pages.
- `web/production-tests/round-trip.spec.ts`: with saved figures on, switch on, offline pages (each answer checked as the
  worker's), renewal after the connection returns, removal on a saved page and a sign-out without a connection; with
  them withdrawn, no switch and no days read. `web/tests/support/worker-state.ts` tells the three states apart: saved
  figures on, saved figures withdrawn with the worker kept, the worker withdrawn.
- `tests/test_plant_days.py`: the days read.

## Device checklist

On Edge (Windows), Chrome (Android) and Safari (iOS, iPadOS 26 and 27, macOS with Add to Dock), for each release that
touches the worker, the switch or sign-in:

1. The offer appears once in the installed window, and not in a browser tab.
2. Turn on, open a plant, then airplane mode: launch from the icon after the app was closed; read a plant that was never
   opened, both kinds and another day; see the curve and the read time.
3. Connection back: "Try again" or a link opens the application, and after the next read the figures are the server's.
4. "Remove saved figures" works in airplane mode.
5. After a sign-out no saved page appears, also through Back in a window that showed one.
6. On iOS: the installed app after two weeks unused, and a Safari tab after a week of other browsing.
7. Record what `navigator.storage.persist()` answers per browser.

## Withdrawal

- Offline reading alone: a release that sets `SAVED_FIGURES` in `web/src/lib/saved/feature.ts` to `false` and serves
  the Phase 3 worker (the worker without the reader and the saved pages); the tests then run in the state "saved
  figures withdrawn". Every document then deletes the database and forgets the flag at its start, the switch and the
  offer render nothing and no refresh runs. Keep the deletion at start for at least one further release, 90 days later,
  because a window of the withdrawn build can still turn the switch on until its session ends.
- The worker: the withdrawal of `docs/DEPLOYMENT.md`, "App worker", together with `SAVED_FIGURES` off, so no copy
  remains without a reader.
