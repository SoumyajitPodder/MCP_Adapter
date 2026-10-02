// lifecycle-controller: public entry point.
//
//   import { createController, createHttpJsonConnector } from 'lifecycle-controller';
//
// See README.md for the connector contract and the controller API.

export { createController } from './controller.js';
export * from './connectors/index.js';
export { primary, versionLabel, adapterLabel, DAY_MS, slugify } from './utils.js';
