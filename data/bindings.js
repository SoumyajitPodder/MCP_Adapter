// Static description of each binding: which tool it serves, what kind of
// upstream backs it (REST or FILE), and the identifiers the simulator uses
// to generate fake records for it. This is the "which bindings exist"
// configuration referenced in the LLD; it does not change at runtime.

export const BINDING_DEFS = [
  {
    id: 'order.get', tool: 'order.get', displayName: 'Order API', kind: 'REST',
    system: 'order-management', iface: 'GET /orders/{id}',
    prefix: 'ORD-', altStatus: 'state',
    newState: { truth: 'awaiting_approval', up: 'WAITING_APPROVAL' },
    ver: 'v2'
  },
  {
    id: 'service.get', tool: 'service.get', displayName: 'Service API', kind: 'REST',
    system: 'service-inventory', iface: 'GET /services/{id}',
    prefix: 'SVC-', altStatus: 'svcState',
    newState: { truth: 'pending_activation', up: 'PENDING_ACT' },
    ver: 'v2'
  },
  {
    id: 'inventory.snapshot', tool: 'inventory.snapshot', displayName: 'Inventory Feed', kind: 'FILE',
    system: 'inventory-feed', iface: 'inventory_YYYYMMDD.csv',
    prefix: 'ITM-', altStatus: 'stock_state',
    newState: { truth: 'in_transit', up: 'IN_TRANSIT' },
    ver: 'v1'
  }
];
