# Orbs from other libraries

Built files from two MIT-licensed libraries. Don't edit them; rebuild with `tools/orbs/build.sh`.

- **VoiceOrbs** by Alexis Munoz, https://github.com/amunozdev/voiceorbs (commit 8b27999, 28 September 2026).
  `voiceorbs.js` and `voiceorbs.css` hold the 20 orbs as React components with React bundled in, and
  `mountOrb(element, id, props)` to put one on a plain element. Nebula's three.js scene is a separate
  file that loads only when Nebula is picked. One change from upstream: Waveform Ring traces the page's
  own audio (`useAnalyser`) instead of asking for the microphone a second time.
- **Thinking Orbs** by Jakub Antalik, https://github.com/Jakubantalik/Libraries.dev (commit bad6322,
  27 October 2026). `thinking-orbs.js` is the package's canvas engine and nine tuned states without
  React, with `thinkingOrb(canvas, options)` to draw one.

`static/orb-skins.js` is the lab's side: the orb picker's list, how live mode's states map onto
VoiceOrbs' seven, and the small orbs beside tool steps.
