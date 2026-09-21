(() => {
  const config = __MBA_CONFIG__;
  // Playwright does not define ordering among multiple page init scripts.
  // A prior binding must never replace a newer binding after navigation.
  if ((window.__mbaObservations?.bindingVersion || 0) > (config._binding_version || 0)) return;
  if (window.__mbaObservations?.configKey === JSON.stringify(config)) return;
  window.__mbaObservations?.observer?.disconnect();
  const state = {pending: [], dropped: 0, error: null, configKey: JSON.stringify(config), bindingVersion: config._binding_version || 0};
  window.__mbaObservations = state;
  const documentId = window.__mbaDocumentId ||= (crypto.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint8Array(16)), v => v.toString(16).padStart(2, '0')).join(''));
  const nodeIds = new WeakMap();
  const revisions = new Map();
  let previous = new Map(), counter = 0;
  function publish(event) {
    if (state.pending.length >= 1000) { state.pending.shift(); state.dropped++; }
    state.pending.push({...event, at: performance.now(), pageUrl: location.href});
  }
  function scan() {
    try {
      const current = new Map();
      for (const node of document.querySelectorAll(config.message_selector)) {
        if (!nodeIds.has(node)) nodeIds.set(node, `node-${++counter}`);
        const id = `${documentId}:${node.getAttribute(config.message_id_attribute) || nodeIds.get(node)}`;
        const content = node.innerText || '';
        current.set(id, content);
        if (!previous.has(id) || previous.get(id) !== content) {
          const revision = (revisions.get(id) || 0) + 1;
          revisions.set(id, revision);
          publish({type: 'text.revision', data: {message_id: id, revision, content, source: 'dom'}});
        }
      }
      for (const id of previous.keys()) {
        if (!current.has(id)) publish({type: 'text.removed', data: {message_id: id}});
      }
      previous = current;
    } catch (error) { state.error = String(error); }
  }
  state.observer = new MutationObserver(scan);
  state.observer.observe(document, {subtree: true, childList: true, characterData: true, attributes: true});
  scan();
})();
