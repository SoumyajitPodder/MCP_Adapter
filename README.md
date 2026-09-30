Lifecycle Controller Prototype

A Phase 1 prototype for the MCP adapter project.

The prototype simulates three tool bindings:

order.get
service.get
inventory.snapshot

Each binding connects to a fake upstream system. You can introduce different kinds of upstream changes and watch the actual drift-detection pipeline respond in real time.

This isn't a mockup or a collection of pre-recorded UI states. The prototype runs the actual detection, classification, remediation, validation, review, and adapter lifecycle logic.

Note: This is a prototype, not a production system. There is no backend, build process, or external dependency. Everything runs directly in the browser using HTML, CSS, and JavaScript modules.

Getting Started
1. Open the prototype

The simplest option is to open index.html in your browser.

However, because the prototype uses JavaScript ES modules, some browsers won't allow the modules to load correctly over file://.

If you see a blank page or errors about CORS/modules, run a local server instead:

bash
python3 -m http.server 8000

Then open:

text
http://localhost:8000
What can you do with it?
The layout

The screen is split into three parts:

A left panel — pick a binding, introduce drift by hand, or add a preselected scenario. This is the "make something happen" side.
The main area — three tabs: Pipeline (the live six-stage check for whatever binding is selected), Mapping (the adapter's current field-by-field mapping), and Audit Log (a searchable history of everything that's happened).
A right panel — anything that needs a decision, and a view into what the system just fixed on its own. This is the "pay attention here" side.

Both side panels pull in and out from a small arrow tab docked to their edge. The right panel opens itself the moment something needs a decision, so you don't have to go looking for it.

Simulate upstream drift

Select a binding and introduce different types of changes, including:

Renaming a field
Adding an enum value
Changing a date format
Removing a required field
Reordering file columns
Changing a file delimiter
Announcing an API version sunset
Forcing an unexpected version cutover
Introducing a silent breaking change

Clicking one of these doesn't change anything immediately — it adds the change to the drift queue in the left panel. It sits there, cancellable, until the next batch run actually applies it. This is deliberate: it gives you a moment to see what's about to happen, and a way to undo a click before it lands.

The goal is to see how the system reacts to each type of change rather than simply displaying a pre-built result.

Preselected scenarios

Beyond one-off changes, the left panel has four scenarios — realistic, multi-day sequences on a single binding:

Planned API deprecation (order.get) — a harmless rename, then a new status value, then an announced retirement.
Vendor housekeeping, then a version retirement (order.get) — a few weeks of ordinary cleanup (a new field, a date format change, a renamed field), ending in a version sunset. This one exists to show that a version change rarely arrives on its own — it's usually one line in a longer changelog.
Breaking change overnight (service.get) — no warning: a type change, then the endpoint is cut over to a new version with no notice at all.
Silent corruption, then a real outage (inventory.snapshot) — a quiet meaning-swap slips in unnoticed; an unrelated breaking change days later is what actually gets someone looking at the feed.

Add scenario queues the whole sequence at once — each step still lands on its own scheduled day and can still be cancelled individually from the drift queue. Only one scenario can be queued per binding at a time, so two scripted stories can't tangle together on the same upstream.

Adding a scenario, or clicking a one-off injector, only queues the upstream side of the story. Approving reviews, standing up adapters, and promoting candidates as the days pass is still up to you — that response is the part worth demoing live.

Watch the drift pipeline

Running a batch checks every active adapter through six stages:

Version check
Schema comparison
Adapter compatibility
Smoke test
Canonical validation
Readiness

Each binding ends up in one of three states:

READY — everything looks good
REVIEW — something changed and needs a decision
BLOCKED — the system cannot safely continue

You can run batches manually, move forward one day at a time, or turn on Auto to let the simulated clock run continuously.

The speed can be adjusted from 0.5× to 4×, which makes it easy to either walk through a scenario slowly or fast-forward through several days.

Seeing what happened

Every batch run is saved.

After running a batch, you can see:

Which bindings were checked
Which upstreams were checked
The result for each binding
The batch number
Previous batch runs

You can also click back through previous runs to see how the system reached its current state.

Every drift event and lifecycle change is also recorded in the Audit Log tab, and it's built to be queried, not just scrolled. Each entry carries:

Who did it — the system, an operator, a reviewer, or the simulator
What happened — a fixed category like a drift absorbed, a review approved, an adapter rolled back, a contract deprecated, or a queued change cancelled
Which binding it touched
Why, in plain language

You can filter by actor or action category, or search free text — this is what answers "who approved this mapping" or "when did that rollback happen and why."

For debugging or demonstrations, the entire prototype can also be controlled from the browser console through:

js
window.__core
How drift is handled

Not every change needs a human.

Safe changes

Changes that can be handled deterministically are absorbed automatically.

For example, if an upstream renames a field and the system can confidently determine the mapping, the adapter can translate the new field back into the canonical format.

Every one of these shows up in the right panel, under Auto-fixed drift — so "the system handled this on its own" isn't just a claim, it's something you can watch happen. A rename shows the same confidence breakdown described below, since the same score is what let it through without a review; anything else (an unwrapped envelope, a reordered column, a coerced type) is a fixed rule with no scoring involved, and the panel says so rather than manufacturing a confidence number for it.

Ambiguous changes

Some changes aren't safe to guess.

For example:

A probable field rename
A new enum value
A date format change

These open a review in the right panel, containing:

The proposed mapping
A sandbox replay
A shadow comparison against the last-known-good result
The information needed to approve or reject the change

The panel is built to be hard to miss when something is waiting on you: it opens itself the moment a review appears, the review's card gets an urgent border, and the panel's pull tab turns red and pulses with a count — even while collapsed. Once resolved, a review shrinks to a single line so it doesn't crowd out anything still waiting on a decision.

For field renames, the prototype also shows how the confidence score was calculated, as a bar chart rather than a single number:

Name similarity — weighted 0.5
Type compatibility — weighted 0.2
Value shape — weighted 0.3

This is the same score that decides whether a rename gets absorbed automatically or sent to review — the point is being able to show why the system made that call, not just state it.

Breaking changes

If the system can't safely map a change, it blocks the affected path instead of guessing.

For breaking changes or announced API sunsets, a new adapter version can be created and moved through:

text
tested → canary → primary

The existing primary adapter stays untouched until the new version has passed the required checks.

The rollback demo

The prototype can also demonstrate what happens when a new adapter looks good initially but fails later.

Try this:

Select order.get.
Click "Announce v2 sunset in 6 days, release v3." It appears in the drift queue in the left panel — nothing has changed yet.
Click Next day. The queued change lands, and the sunset takes effect.
Click "Stand up adapter for v3."
Promote the new adapter to canary.
Click Next day to run a clean canary check.
Click "Simulate canary failure."
Click Next day again.

The next run introduces a correctness bug that wasn't caught during the initial sandbox check.

The canonical validation stage fails, showing the before/after difference, and the system automatically rolls the adapter back.

The resulting lifecycle is:

text
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

The important part is that the original primary adapter was never replaced or demoted.

You can also click Call tool now during or after the failure to verify that the original adapter is still serving requests.

The silent breaking change

There is one scenario that intentionally produces no alert.

Select any binding and choose:

Silent breaking change (new records, meaning swapped)

Then click Next day.

The batch will still report READY.

That's intentional.

The simulated upstream returns data that is still perfectly valid according to the schema. The problem is that the values now mean something different.

For example, a value can still be a valid string or enum while representing the wrong business meaning.

This demonstrates an important limitation of structural drift detection:

Schema and type checks can tell us that data is well-formed. They cannot always tell us that the data still means the same thing.

You can also see this by calling the tool directly:

js
const b = window.__core.getState().bindings[0];
window.__core.liveCall(b);

The response is schema-valid, even though the underlying meaning has changed.

This is intentionally left as a limitation for Phase 1 and is one of the reasons the later design calls for value-level checks in addition to structural checks.

Note that this doesn't show up in the Auto-fixed drift panel either — that panel lists what the system caught and fixed. This is drift the system never saw in the first place, which is the whole point.

What is included

The prototype currently covers the main Phase 1 flow:

Canonical tool contracts
Stable output translation
API drift detection
File drift detection
Drift classification
Automatic absorption of known-safe changes, visible with a confidence descriptor
A drift queue: manual injections and scenario steps alike wait until the next batch run, and can be cancelled before they land
Preselected multi-day scenarios, including one that mixes a version drift in with ordinary changes
Human review for ambiguous changes, in an attention-grabbing dedicated panel
Sandbox replay
Shadow comparison
Canonical validation
Adapter lifecycle management
Canary testing
Automatic rollback
Batch processing
Runtime fail-closed behavior
A queryable audit trail (actor, action, binding, and reason for every event)
Browser-console access through window.__core

The following are intentionally out of scope for this prototype:

Write-path tools
Queue-based upstream adapters (e.g. Kafka/SQS-style feeds — unrelated to the drift queue described above, which is a UI concept, not an upstream type)
An LLM-based mapping proposer
Real external APIs
Production infrastructure

The idea is to demonstrate the core behavior without building the entire production system around it.

Project Structure
text
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
    ├── bindings.js
    └── scenarios.js
What each part does

app.js Starts the prototype, wires the different layers together, and exposes window.__core for console access.

state.js Owns the prototype's mutable state and logging.

core/ Contains the actual lifecycle and drift logic.

utils.js — shared helper functions
detector.js — detects changes between upstream data and the stored baseline
classifier.js — determines whether a change is compatible, requires review, is breaking, or is unknown
validator.js — applies mappings and checks the result against the canonical contract
adapters.js — creates adapters and captures baselines
lifecycle.js — handles batches, reviews, promotion, retirement, rollback, runtime calls, and the drift queue (both scenario steps and manual injections land through the same mechanism)

simulation/upstreams.js Contains the fake upstream systems and the controls used to inject drift.

ui/ Handles the browser interface, including both side panels, the tabbed main area, and the audit trail's search and filters.

render.js — turns application state into HTML
events.js — handles user interactions
dom.js — small DOM helpers

data/ Defines the canonical contracts, the bindings between tools and simulated upstreams, and the preselected scenarios.

Architecture

The prototype intentionally keeps the UI separate from the actual drift logic.

At a high level:

text
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

The core logic does not directly manipulate the DOM.

In particular, detector.js, classifier.js, and validator.js are plain functions. They take data in and return results.

The UI layer is responsible for displaying those results.

This separation keeps the prototype easy to reason about and makes the core behavior possible to exercise without going through the UI.

Dependency flow

The dependency structure is intentionally simple:

text
data
  ↓
state
  ↓
core / simulation
  ↓
app
  ↓
ui

core/ and simulation/ work through state.js rather than directly manipulating the browser.

Only the UI layer interacts with document.

app.js acts as the composition root: it is the one place that brings the different pieces together and starts the application.