const { ipcRenderer } = require('electron')
// Wheel gestures aren't consistently surfaced by before-mouse-event on Windows.
// This preload only relinquishes control; it exposes no desktop API to page scripts.
for (const name of ['wheel', 'touchstart']) {
  window.addEventListener(name, event => {
    if (event.isTrusted) ipcRenderer.send('browser:human-input')
  }, { capture: true, passive: true })
}
