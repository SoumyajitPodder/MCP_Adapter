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
- A day and time readout in the header. "Next day" and "Run batch" still
  jump a full day at a time; "Auto" ticks the clock continuously between
  batches, and the speed selector (0.5× to 4×) controls how fast — useful
  for either a slow, deliberate walkthrough or a fast-forwarded multi-day
  view.
- Buttons to inject drift into whichever binding is selected: rename a
  field, add an enum value, flip a date format, remove a required column,
  reorder file columns, change a delimiter, announce a graceful version
  sunset, force an unannounced overnight version cutover, or introduce a
  silent breaking change.
- The silent breaking change is deliberately the odd one out: it doesn't
  get flagged. New records report an ordinary status using an
  already-known code, just the wrong one, with no prior history to catch
  the mismatch against. The batch shows READY the whole time. That's the
  point — it's the blind spot no amount of schema diffing catches, and the
  reason the LLD calls for value-level checks, not just shape-level ones.
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
- A probable rename shows its confidence as a bar chart, not just a bare
  number: the overall score, plus the three components that make it up
  (name similarity, type compatibility, value shape) exactly as the
  classifier weighs them (0.5 / 0.2 / 0.3). This is the same number that
  decides auto-absorb vs. review vs. block; the point is being able to
  show, not just state, why the system made that call.
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

## Demoing the silent breaking change

This one has the opposite punchline from the rollback story: nothing
happens, and that's the problem.

1. Select any binding and click "Silent breaking change (new records,
   meaning swapped)."
2. Click "Next day." The batch overview still shows READY. No review
   opens. The log line only tells you what was injected, because nothing
   in the pipeline noticed it on its own.
3. Open a browser console and run:
   ```js
   const b = window.__core.getState().bindings[0];
   window.__core.liveCall(b);
   ```
   The data comes back clean and schema-valid — the corruption is in the
   values, not the shape, so nothing here flags it either.

The point isn't a bug in the prototype. It's the LLD's own finding:
schema and type diffing cannot catch a value that quietly means something
different while staying perfectly well-formed. That's the argument for
value-level checks in a later phase, not just structural ones.

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

## How the pieces depend on each other

`core/utils.js` has no dependencies. `state.js` depends only on `data/`
and `core/utils.js`. Everything in `core/` and `simulation/` reads and
writes through `state.js`, but nothing in `core/` touches the DOM —
`detector.js`, `classifier.js`, and `validator.js` in particular are
plain functions: given a binding, an adapter, and some data, they return
a result. `ui/render.js` and `ui/events.js` are the only files that touch
`document`. `app.js` is the only file that imports from every layer; it's
the one place that wires the whole thing together and boots it.
