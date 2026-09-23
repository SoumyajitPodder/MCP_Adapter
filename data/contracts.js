// Canonical contract definitions for the three Phase 1 tools.
// This is configuration data only. Nothing in this file runs any logic —
// it just describes what each tool means, per the LLD's "canonical
// contract" section.

export function makeContracts() {
  return {
    'order.get': {
      version: '1.2.0', state: 'ACTIVE', sunsetDay: null,
      fields: [
        { name: 'order_id', type: 'string' },
        { name: 'status', type: 'enum', values: ['in_progress', 'completed', 'cancelled'] },
        { name: 'customer_id', type: 'string' },
        { name: 'created_at', type: 'datetime' }
      ]
    },
    'service.get': {
      version: '1.0.0', state: 'ACTIVE', sunsetDay: null,
      fields: [
        { name: 'service_id', type: 'string' },
        { name: 'status', type: 'enum', values: ['active', 'suspended', 'terminated'] },
        { name: 'account_id', type: 'string' },
        { name: 'activated_at', type: 'datetime' }
      ]
    },
    'inventory.snapshot': {
      version: '1.0.0', state: 'ACTIVE', sunsetDay: null,
      fields: [
        { name: 'item_id', type: 'string' },
        { name: 'status', type: 'enum', values: ['available', 'reserved', 'decommissioned'] },
        { name: 'site_id', type: 'string' },
        { name: 'updated_at', type: 'datetime' }
      ]
    }
  };
}
