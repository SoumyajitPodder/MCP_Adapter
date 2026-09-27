# Lifecycle Controller Prototype

A Phase 1 prototype for the MCP adapter project.

The prototype simulates three tool bindings:

* `order.get`
* `service.get`
* `inventory.snapshot`

Each binding connects to a fake upstream system. You can introduce different kinds of upstream changes and watch the actual drift-detection pipeline respond in real time.

This isn't a mockup or a collection of pre-recorded UI states. The prototype runs the actual detection, classification, remediation, validation, review, and adapter lifecycle logic.

> **Note:** This is a prototype, not a production system. There is no backend, build process, or external dependency. Everything runs directly in the browser using HTML, CSS, and JavaScript modules.

## Getting Started

### 1. Open the prototype

The simplest option is to open `index.html` in your browser.

However, because the prototype uses JavaScript ES modules, some browsers won't allow the modules to load correctly over `file://`.

If you see a blank page or errors about CORS/modules, run a local server instead:

```bash
python3 -m http.server 8000
```

Then open:

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
│   │   ├── utils.js
│   │   ├── detector.js
│   │   ├── classifier.js
│   │   ├── validator.js
│   │   ├── adapters.js
│   │   └── lifecycle.js
│   │
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


