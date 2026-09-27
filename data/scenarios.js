// Preselected scenarios: realistic, multi-day sequences of upstream events,
// each targeting one binding. Activating a scenario only queues the
// upstream-side events (what happens to the vendor's API or file feed) at
// specific future days — it does not script the operator's response.
// Approving reviews, standing up adapters, and promoting candidates stays
// interactive, because that's the half of the story worth demoing live.
//
// `offset` is measured in days from the day the scenario is activated, not
// an absolute day number, so a scenario behaves the same whether it's
// started on day 0 or day 12. `injectId` must match an id in INJ
// (js/simulation/upstreams.js).

export const SCENARIOS = [
  {
    id: 'planned_deprecation',
    name: 'Planned API deprecation',
    bindingId: 'order.get',
    description: 'A vendor evolves their API responsibly: a harmless rename, then a new status value, then an announced retirement.',
    steps: [
      { offset: 0, injectId: 'rename_case', note: 'field renamed to snake_case' },
      { offset: 2, injectId: 'new_enum', note: 'a new order status appears' },
      { offset: 4, injectId: 'sunset', note: 'v2 sunset announced, v3 released' }
    ]
  },
  {
    id: 'overnight_break',
    name: 'Breaking change overnight',
    bindingId: 'service.get',
    description: 'No warning this time: a field quietly changes type, then the endpoint is cut over to a new version overnight.',
    steps: [
      { offset: 0, injectId: 'type_change', note: 'account id starts arriving as a number' },
      { offset: 1, injectId: 'version_bump', note: 'v2 is gone, replaced without notice' }
    ]
  },
  {
    id: 'silent_corruption',
    name: 'Silent corruption, then a real outage',
    bindingId: 'inventory.snapshot',
    description: 'A quiet meaning-swap slips in undetected. Days later an unrelated breaking change is what finally gets someone looking at the feed.',
    steps: [
      { offset: 0, injectId: 'add_optional', note: 'a harmless new column appears' },
      { offset: 2, injectId: 'silent_swap', note: 'new records start reporting the wrong status — nothing flags it' },
      { offset: 5, injectId: 'remove_required', note: 'a required column disappears; now it breaks loudly' }
    ]
  }
];
