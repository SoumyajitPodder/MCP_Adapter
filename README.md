# Lifecycle Controller Prototype

Phase 1 prototype for the MCP adapter project. Simulates three tool
bindings (order.get, service.get, inventory.snapshot) with fake upstreams,
and runs the actual detect -> classify -> absorb / review / block logic
against them, so you can inject a real drift scenario and watch the
adapter pipeline react to it instead of looking at a mocked-up UI.

This is a prototype, not a production build. No backend, no build step,
no dependencies. It's plain HTML, CSS, and JS modules that run directly in
the browser.

## How to run it

Open `index.html` in a browser.

Because it uses ES module imports (`<script type="module">`), some
browsers block module loading over the `file://` protocol. If double
clicking the file gives you a blank page or console errors about CORS or
modules, serve the folder instead:

```
cd prototype
python3 -m http.server 8000
```

Then open `http://localhost:8000` in the browser.

## What it does

- Three bindings, each with a primary adapter already serving a
  "healthy" upstream.
- Buttons to inject drift into whichever binding is selected: rename a
  field, add an enum value, flip a date format, remove a required column,
  reorder file columns, change a delimiter, or announce a version sunset.
- Run a batch (or step day by day, or hit auto-run) to trigger the
  six-stage sanity pipeline against every active adapter: version check,
  schema comparison, adapter compatibility, smoke test, canonical
  validation, readiness.
- Every batch run is recorded. Hitting "Run batch" or "Next day" shows a
  batch run overview: a numbered run, how many bindings and sources were
  checked, and a READY / REVIEW / BLOCKED result per binding, with a
  history strip underneath so you can click back through past runs.
- A canary can be made to fail on purpose, to show what happens when a
  new adapter is worse, not just when one is better. Once a candidate is
  promoted to canary, a "Simulate canary failure" button queues a
  correctness bug that only shows up on the next run (representing a bug
  the initial sandbox check didn't catch). That run fails canonical
  validation with a clear before/after diff, and the adapter is
  automatically rolled back: candidate -> sandbox pass -> canary -> failure
  -> rollback -> previous primary. The old primary is never touched or
  demoted, and a live call during and after the failure still confirms
  it's the one being served.
- Ambiguous drift (a probable rename, a new enum value, a date format
  change) opens a review card with a proposed mapping, a sandbox replay
  result, and a shadow comparison against last-known-good output. You
  pick what an unknown value means and approve or reject.
- Breaking drift or an announced sunset lets you stand up a new adapter
  version, which goes through tested -> canary -> primary before it
  takes over, same as a real rollout would.
- "Call tool now" runs the same detect/classify logic on the runtime
  path and fails closed if it can't produce a safe answer, instead of
  guessing.
- A log of every drift event and lifecycle transition, and a
  `window.__core` object in devtools for driving the whole thing from the
  console instead of clicking buttons.

Everything above matches the Phase 1 LLD (canonical contract, stable
translation, API/file drift adapters, lifecycle controller). Write-path
tools, the queue adapter, and an LLM-based mapping proposer are
deliberately not in this prototype, same as the LLD scopes them out for
now.

## Demoing the rollback story

The rest of the prototype is mostly about promotion: drift comes in, it
gets absorbed or reviewed, and a good candidate eventually becomes
primary. This walks through the other half: a candidate that looks fine
at first and turns out to be worse.

1. Select `order.get` and click "Announce v2 sunset in 6 days, release
   v3" in the simulate panel.
2. Click "Next day" once. Still healthy, just a REVIEW warning that v2 is
   being retired.
3. Click "Stand up adapter for v3." It comes up in `tested` and passes
   immediately (that's the sandbox check).
4. On that new adapter's tab in the lifecycle rail, click "Promote to
   canary."
5. Click "Next day." One clean canary run.
6. Click "Simulate canary failure" on the canary chip. This queues a bug
   that the sandbox pass and the first canary run didn't catch.
7. Click "Next day" again. The sanity pipeline shows the canonical
   validation stage failing, with the exact before/after value that broke
   it, and the adapter moves itself into "Rolled back."
8. Click "Call tool now." It's still being served by the original
   primary, unaffected, the whole time.

That's the sequence: primary -> candidate -> canary -> failure ->
rollback -> previous primary, and it's real code running it, not a
canned animation.

## Folder layout

```
prototype/
├── index.html              page shell, loads css/js
├── css/
│   └── styles.css
├── js/
│   ├── app.js               composition root: builds bindings, wires
│   │                         everything up, exposes window.__core
│   ├── state.js              the one mutable state object, plus logging
│   ├── core/
│   │   ├── utils.js          generic helpers (string similarity, date
│   │   │                     formatting, etc), no dependencies
│   │   ├── detector.js        detect stage: diffs a response against
│   │   │                     the stored baseline
│   │   ├── classifier.js      classify stage: turns detected diffs into
│   │   │                     COMPATIBLE / REVIEW_REQUIRED / BREAKING /
│   │   │                     UNKNOWN
│   │   ├── validator.js       translate a mapping against records, check
│   │   │                     the result against the canonical contract
│   │   ├── adapters.js        adapter creation and baseline capture
│   │   └── lifecycle.js       the six-stage pipeline, batch history,
│   │                         review workflow, promotion/retirement/
│   │                         rollback, the live-call path
│   ├── simulation/
│   │   └── upstreams.js       fake upstream data plus the drift
│   │                         injection buttons
│   └── ui/
│       ├── dom.js             two tiny DOM helpers
│       ├── render.js           turns state into markup, no logic
│       └── events.js           click/change handlers, calls into core/
└── data/
    ├── contracts.js           the three canonical tool contracts
    └── bindings.js            which tool maps to which fake upstream
```

Layout note: the task sketch had `core/adapters.js` but not a `ui/`
folder. I split rendering (`render.js`) from event wiring (`events.js`)
and added `dom.js` for the two shared DOM helpers, since keeping all of
that in one file made it hard to tell "what draws the screen" apart from
"what happens when you click something." I also pulled the handful of
generic helpers (string similarity, date parsing, etc.) into
`core/utils.js` rather than leaving them scattered at the top of the old
single file, since several modules need them and none of them belong to
any one module in particular.

## How the pieces depend on each other

`core/utils.js` has no dependencies. `state.js` depends only on `data/`
and `core/utils.js`. Everything in `core/` and `simulation/` reads and
writes through `state.js`, but nothing in `core/` touches the DOM —
`detector.js`, `classifier.js`, and `validator.js` in particular are
plain functions: given a binding, an adapter, and some data, they return
a result. `ui/render.js` and `ui/events.js` are the only files that touch
`document`. `app.js` is the only file that imports from every layer; it's
the one place that wires the whole thing together and boots it.

That's also why the modules were testable on their own: before wiring up
the UI, I ran the whole detect/classify/absorb/promote flow headlessly in
Node, no browser needed, since none of that code depends on a DOM. That's
the separation the task asked for, not just a style preference.

## What changed from the single-file version

Nothing functional. Every function in `core/` and `simulation/` is the
same logic, moved into its file and given real imports/exports instead
of living in one closure. The only actual changes:

- Small helper functions (`clone`, `nameSim`, date formatting, etc.) were
  pulled out into `core/utils.js` since they were previously just sitting
  at the top of the file with everything else.
- `primary()` (find a binding's current live adapter) lives in
  `core/utils.js` instead of next to the other adapter functions, because
  both `core/adapters.js` and `simulation/upstreams.js` need it, and
  having them import each other would create a circular dependency.
- The event handlers take a callback for the Reset button
  (`bindEvents({ onReset })`) instead of calling the bootstrap function
  directly, so `events.js` doesn't need to import `app.js`.

No behavior was added or removed in that pass. Every drift scenario,
every button, and `window.__core` all worked exactly like they did before
the split.

Batch history and the canary failure/rollback story (described above)
were added afterward, as an actual feature addition on top of the
modular structure — new lifecycle state, new lifecycle transition, new
UI panel — not part of the file reorganization itself.
