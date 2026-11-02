#!/bin/bash
# Rebuilds static/vendor/orbs from the two orb libraries. Needs Node and git.
#   tools/orbs/build.sh            (from the repo root)
# VoiceOrbs   github.com/amunozdev/voiceorbs           MIT, (c) 2026 Alexis Munoz
# Thinking Orbs github.com/Jakubantalik/Libraries.dev  MIT, (c) 2026 Jakub Antalik
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../../static/vendor/orbs"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
VOICEORBS_REV=8b2799917734f52c9293a489e08982ca5b0187e9
LIBRARIES_REV=bad63221ebafa08d830586aa1721ecbd4ea02fcb

cd "$WORK"
git clone -q https://github.com/amunozdev/voiceorbs && git -C voiceorbs checkout -q $VOICEORBS_REV
git clone -q https://github.com/Jakubantalik/Libraries.dev libraries && git -C libraries checkout -q $LIBRARIES_REV
mkdir build && cd build
cp "$HERE"/entry.tsx "$HERE"/thinking-entry.ts "$HERE"/tsconfig.json .
mkdir shims && cp "$HERE"/next-dynamic.tsx shims/
npm init -y >/dev/null
npm install --ignore-scripts --no-audit --no-fund -s react@19.2.4 react-dom@19.2.4 three@0.185.0 \
  @react-three/fiber@9.6.1 @paper-design/shaders-react@0.0.76 clsx@2.1.1 esbuild@0.28.2
mkdir -p src && cp -R ../voiceorbs/src/registry src/registry
# Waveform Ring asks for the microphone itself; let the page hand it an analyser instead
python3 - <<'PY'
from pathlib import Path
p = Path("src/registry/lib/use-audio-level.ts"); s = p.read_text()
old = "export const acquireSharedAnalyser = (): Promise<AnalyserNode> => {\n"
assert old in s
p.write_text(s.replace(old, old + "  const own = (globalThis as any).__voiceOrbsAnalyser?.();\n  if (own) return Promise.resolve(own as AnalyserNode);\n"))
PY
VO_BANNER='/* VoiceOrbs (github.com/amunozdev/voiceorbs), MIT, (c) 2026 Alexis Munoz; bundled with React, three.js and Paper Shaders (MIT). See LICENSE-voiceorbs. */'
TO_BANNER='/* Thinking Orbs (github.com/Jakubantalik/Libraries.dev), MIT, (c) 2026 Jakub Antalik. See LICENSE-thinking-orbs. */'
rm -rf "$OUT" && mkdir -p "$OUT"
./node_modules/.bin/esbuild entry.tsx --bundle --format=esm --splitting --outdir="$OUT" --minify --jsx=automatic \
  '--loader:.module.css=local-css' '--define:process.env.NODE_ENV="production"' --tsconfig=tsconfig.json \
  --entry-names=voiceorbs '--chunk-names=voiceorbs-[name]-[hash]' --legal-comments=eof \
  "--banner:js=$VO_BANNER" "--banner:css=$VO_BANNER" --log-level=warning
./node_modules/.bin/esbuild thinking-entry.ts --bundle --format=esm --minify --outfile="$OUT/thinking-orbs.js" \
  --legal-comments=eof "--banner:js=$TO_BANNER" --log-level=warning
cp ../voiceorbs/LICENSE "$OUT/LICENSE-voiceorbs"
cp ../libraries/LICENSE "$OUT/LICENSE-thinking-orbs"
ls -la "$OUT"
