# Lifecycle Controller Prototype

A Phase 1 prototype for the MCP adapter project.

The prototype simulates three tool bindings:

* `order.get`
* `service.get`
* `inventory.snapshot`

Each binding connects to a fake upstream system. You can introduce different kinds of upstream changes and watch the actual drift-detection pipeline respond in real time.

This isn't a mockup or a collection of pre-recorded UI states. The prototype runs the actual detection, classification, remediation, validation, review, and adapter lifecycle logic.

<<<<<<< HEAD
> **Note:** This is a prototype, not a production system. There is no backend, build process, or external dependency. Everything runs directly in the browser using HTML, CSS, and JavaScript modules.

## Getting Started

### 1. Open the prototype

The simplest option is to open `index.html` in your browser.

However, because the prototype uses JavaScript ES modules, some browsers won't allow the modules to load correctly over `file://`.

If you see a blank page or errors about CORS/modules, run a local server instead:

```bash
=======
```
>>>>>>> 65080c04c350f0163a71689589d5137d010e7105
cd prototype
python3 -m http.server 8000
```

<<<<<<< HEAD
Then open:
=======
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
>>>>>>> 65080c04c350f0163a71689589d5137d010e7105

```text
http://localhost:8000
```

## What can you do with it?

### Simulate upstream drift

Select a binding and introduce different types of changes, including:

* Renaming a field
* Adding an enum value
* Changing a date format
* Removing a required field
* Reordering file columns
* Changing a file delimiter
* Announcing an API version sunset
* Forcing an unexpected version cutover
* Introducing a silent breaking change

The goal is to see how the system reacts to each type of change rather than simply displaying a pre-built result.

### Watch the drift pipeline

Running a batch checks every active adapter through six stages:

1. **Version check**
2. **Schema comparison**
3. **Adapter compatibility**
4. **Smoke test**
5. **Canonical validation**
6. **Readiness**

Each binding ends up in one of three states:

* **READY** — everything looks good
* **REVIEW** — something changed and needs a decision
* **BLOCKED** — the system cannot safely continue

You can run batches manually, move forward one day at a time, or turn on **Auto** to let the simulated clock run continuously.

The speed can be adjusted from `0.5×` to `4×`, which makes it easy to either walk through a scenario slowly or fast-forward through several days.

## Seeing what happened

Every batch run is saved.

After running a batch, you can see:

* Which bindings were checked
* Which upstreams were checked
* The result for each binding
* The batch number
* Previous batch runs

You can also click back through previous runs to see how the system reached its current state.

The prototype also keeps a log of drift events and adapter lifecycle changes.

For debugging or demonstrations, the entire prototype can also be controlled from the browser console through:

```js
window.__core
```

## How drift is handled

Not every change needs a human.

### Safe changes

Changes that can be handled deterministically are absorbed automatically.

For example, if an upstream renames a field and the system can confidently determine the mapping, the adapter can translate the new field back into the canonical format.

### Ambiguous changes

Some changes aren't safe to guess.

For example:

* A probable field rename
* A new enum value
* A date format change

These open a review card containing:

* The proposed mapping
* A sandbox replay
* A shadow comparison against the last-known-good result
* The information needed to approve or reject the change

For field renames, the prototype also shows how the confidence score was calculated:

* **Name similarity:** `0.5`
* **Type compatibility:** `0.2`
* **Value shape:** `0.3`

This makes the classification explainable instead of showing only a final score.

### Breaking changes

If the system can't safely map a change, it blocks the affected path instead of guessing.

For breaking changes or announced API sunsets, a new adapter version can be created and moved through:

```text
tested → canary → primary
```

The existing primary adapter stays untouched until the new version has passed the required checks.

## The rollback demo

The prototype can also demonstrate what happens when a new adapter looks good initially but fails later.

Try this:

1. Select `order.get`.
2. Click **"Announce v2 sunset in 6 days, release v3."**
3. Click **Next day**.
4. Click **"Stand up adapter for v3."**
5. Promote the new adapter to **canary**.
6. Click **Next day** to run a clean canary check.
7. Click **"Simulate canary failure."**
8. Click **Next day** again.

The next run introduces a correctness bug that wasn't caught during the initial sandbox check.

The canonical validation stage fails, showing the before/after difference, and the system automatically rolls the adapter back.

The resulting lifecycle is:

```text
primary
   ↓
candidate
   ↓
canary
   ↓
failure
   ↓
rollback
   ↓
previous primary
```

The important part is that the original primary adapter was never replaced or demoted.

You can also click **Call tool now** during or after the failure to verify that the original adapter is still serving requests.

## The silent breaking change

There is one scenario that intentionally produces **no alert**.

Select any binding and choose:

> **Silent breaking change (new records, meaning swapped)**

Then click **Next day**.

The batch will still report **READY**.

That's intentional.

The simulated upstream returns data that is still perfectly valid according to the schema. The problem is that the values now mean something different.

For example, a value can still be a valid string or enum while representing the wrong business meaning.

This demonstrates an important limitation of structural drift detection:

> Schema and type checks can tell us that data is well-formed. They cannot always tell us that the data still means the same thing.

You can also see this by calling the tool directly:

```js
const b = window.__core.getState().bindings[0];
window.__core.liveCall(b);
```

The response is schema-valid, even though the underlying meaning has changed.

This is intentionally left as a limitation for Phase 1 and is one of the reasons the later design calls for value-level checks in addition to structural checks.

## What is included

The prototype currently covers the main Phase 1 flow:

* Canonical tool contracts
* Stable output translation
* API drift detection
* File drift detection
* Drift classification
* Automatic absorption of known-safe changes
* Human review for ambiguous changes
* Sandbox replay
* Shadow comparison
* Canonical validation
* Adapter lifecycle management
* Canary testing
* Automatic rollback
* Batch processing
* Runtime fail-closed behavior
* Drift and lifecycle logging
* Browser-console access through `window.__core`

The following are intentionally **out of scope for this prototype**:

* Write-path tools
* Queue adapters
* An LLM-based mapping proposer
* Real external APIs
* Production infrastructure

The idea is to demonstrate the core behavior without building the entire production system around it.

## Project Structure

```text
prototype/
├── index.html
├── css/
│   └── styles.css
│
├── js/
│   ├── app.js
│   ├── state.js
│   │
│   ├── core/
<<<<<<< HEAD
│   │   ├── utils.js
│   │   ├── detector.js
│   │   ├── classifier.js
│   │   ├── validator.js
│   │   ├── adapters.js
│   │   └── lifecycle.js
│   │
=======
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
>>>>>>> 65080c04c350f0163a71689589d5137d010e7105
│   ├── simulation/
│   │   └── upstreams.js
│   │
│   └── ui/
│       ├── dom.js
│       ├── render.js
│       └── events.js
│
└── data/
    ├── contracts.js
    └── bindings.js
```

<<<<<<< HEAD
### What each part does

**`app.js`**
Starts the prototype, wires the different layers together, and exposes `window.__core` for console access.

**`state.js`**
Owns the prototype's mutable state and logging.

**`core/`**
Contains the actual lifecycle and drift logic.

* `utils.js` — shared helper functions
* `detector.js` — detects changes between upstream data and the stored baseline
* `classifier.js` — determines whether a change is compatible, requires review, is breaking, or is unknown
* `validator.js` — applies mappings and checks the result against the canonical contract
* `adapters.js` — creates adapters and captures baselines
* `lifecycle.js` — handles batches, reviews, promotion, retirement, rollback, and runtime calls

**`simulation/upstreams.js`**
Contains the fake upstream systems and the controls used to inject drift.

**`ui/`**
Handles the browser interface.

* `render.js` — turns application state into HTML
* `events.js` — handles user interactions
* `dom.js` — small DOM helpers

**`data/`**
Defines the canonical contracts and the bindings between tools and simulated upstreams.

## Architecture

The prototype intentionally keeps the UI separate from the actual drift logic.

At a high level:

```text
                  ┌─────────────────────┐
                  │       Browser       │
                  │     UI / Events     │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │       Core          │
                  │                     │
                  │ Detect → Classify   │
                  │   → Absorb/Review   │
                  │   → Validate        │
                  │   → Lifecycle       │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │    Simulation       │
                  │  Fake Upstreams     │
                  └─────────────────────┘
```

The core logic does not directly manipulate the DOM.

In particular, `detector.js`, `classifier.js`, and `validator.js` are plain functions. They take data in and return results.

The UI layer is responsible for displaying those results.

This separation keeps the prototype easy to reason about and makes the core behavior possible to exercise without going through the UI.

## Dependency flow

The dependency structure is intentionally simple:

```text
data
  ↓
state
  ↓
core / simulation
  ↓
app
  ↓
ui
```

`core/` and `simulation/` work through `state.js` rather than directly manipulating the browser.

Only the UI layer interacts with `document`.

`app.js` acts as the composition root: it is the one place that brings the different pieces together and starts the application.


=======
## How the pieces depend on each other

`core/utils.js` has no dependencies. `state.js` depends only on `data/`
and `core/utils.js`. Everything in `core/` and `simulation/` reads and
writes through `state.js`, but nothing in `core/` touches the DOM —
`detector.js`, `classifier.js`, and `validator.js` in particular are
plain functions: given a binding, an adapter, and some data, they return
a result. `ui/render.js` and `ui/events.js` are the only files that touch
`document`. `app.js` is the only file that imports from every layer; it's
the one place that wires the whole thing together and boots it.
>>>>>>> 65080c04c350f0163a71689589d5137d010e7105
