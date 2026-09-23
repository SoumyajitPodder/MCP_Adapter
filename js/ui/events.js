// Wires DOM events (button clicks, the review dropdowns) to the core
// actions and the simulator, then re-renders. This is the only file that
// listens on the document; render.js only ever produces markup.

import { $ } from './dom.js';
import { state } from '../state.js';
import { inject } from '../simulation/upstreams.js';
import {
  runBatch, liveCall, standUp, operatorMap,
  promote, retire, deprecateContract, approve, reject, injectCanaryFault
} from '../core/lifecycle.js';
import { render, animate } from './render.js';

let autoTimer = null;

// onReset is passed in by app.js rather than imported directly, so this
// file never needs to import the composition root.
export function bindEvents({ onReset }) {
  document.addEventListener('click', e => {
    const t = e.target.closest('[data-act]');
    if (!t || t.tagName === 'SELECT') return;
    const act = t.dataset.act, arg = t.dataset.arg;
    const b = state.bindings.find(x => x.id === state.sel);

    switch (act) {
      case 'run': runBatch(false); state.selStage = null; animate(); break;
      case 'next': runBatch(true); state.selStage = null; animate(); break;
      case 'auto':
        if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
        else { autoTimer = setInterval(() => { runBatch(true); state.selStage = null; animate(); }, 6000); }
        $('#autoBtn').textContent = 'Auto: ' + (autoTimer ? 'on (1 day / 6s)' : 'off');
        $('#autoBtn').classList.toggle('on', !!autoTimer);
        break;
      case 'reset':
        onReset();
        if (autoTimer) { clearInterval(autoTimer); autoTimer = null; $('#autoBtn').textContent = 'Auto: off'; $('#autoBtn').classList.remove('on'); }
        render();
        break;
      case 'sel': state.sel = arg; state.selAdapter = null; state.selStage = null; state.reveal = 6; render(); break;
      case 'seladapter': state.selAdapter = arg; state.selStage = null; render(); break;
      case 'stage': state.selStage = +arg; render(); break;
      case 'inject': inject(b, arg); render(); break;
      case 'call': state.calls[b.id] = liveCall(b); render(); break;
      case 'standup': standUp(b, arg); state.selStage = null; animate(); break;
      case 'opmap': operatorMap(b, arg); state.selStage = null; animate(); break;
      case 'promote': promote(b, b.adapters.find(x => x.id === arg)); render(); break;
      case 'retire': retire(b, b.adapters.find(x => x.id === arg)); render(); break;
      case 'deprecate': deprecateContract(arg); render(); break;
      case 'approve': approve(state.reviews.find(r => r.id === arg)); state.selStage = null; animate(); break;
      case 'reject': reject(state.reviews.find(r => r.id === arg)); render(); break;
      case 'breakcanary': injectCanaryFault(b, b.adapters.find(x => x.id === arg)); render(); break;
      case 'viewbatch': state.selBatch = +arg; render(); break;
    }
  });

  document.addEventListener('change', e => {
    const t = e.target;
    if (t.dataset && t.dataset.act === 'choose') {
      const rv = state.reviews.find(r => r.id === t.dataset.rv);
      rv.choices[+t.dataset.i].selected = t.value || null;
      render();
    }
  });
}
