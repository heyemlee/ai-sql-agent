# Architecture assets

Generated with [Archify](https://github.com/tt-a1i/archify), pinned to upstream commit `5289f6867f048a7450ec5718f58459613a84cf41`.

- `architecture.json`: editable architecture specification; Chinese labels match the author's requested language.
- `architecture.html`: self-contained interactive viewer. Download and open locally; GitHub displays its source.
- `../assets/architecture.svg`: canonical SVG exported by the viewer, embedded in both READMEs.
- `verification.json`: specification/artifact hashes, showcase validation, browser containment, and visual review results.
- `ARCHIFY-LICENSE.txt`: upstream renderer license. Embedded font notices are retained in generated assets.

The diagram is a logical overview, not a microservice deployment topology. Shared model calls, memory reads, HTTP responses, and every audit write are summarized rather than drawn as separate connections. Cross-database execution is orchestrated sequentially; the diagram does not imply database-native federated JOINs.

## Regenerate

From the project root, clone Archify outside the application repository and use the pinned version:

```bash
git clone https://github.com/tt-a1i/archify.git /tmp/ai-sql-readme-archify
git -C /tmp/ai-sql-readme-archify checkout 5289f6867f048a7450ec5718f58459613a84cf41

node /tmp/ai-sql-readme-archify/archify/bin/archify.mjs validate \
  architecture docs/diagrams/architecture.json --quality showcase --json
node /tmp/ai-sql-readme-archify/archify/bin/archify.mjs deliver \
  architecture docs/diagrams/architecture.json docs/diagrams/architecture.html \
  --quality showcase --json
node /tmp/ai-sql-readme-archify/archify/bin/archify.mjs visual-check \
  docs/diagrams/architecture.html --json
```

Open the generated HTML and choose **Export → SVG**, then save the download as `docs/assets/architecture.svg`. Recheck both themes and update `verification.json` after regenerating. Archify is a documentation authoring tool, not an application runtime dependency.

## Replace the screenshot

Add the real console screenshot as `docs/assets/screenshot.png`, preferably in a 16:9 aspect ratio. In both root READMEs, replace `docs/assets/screenshot-placeholder.svg` with `docs/assets/screenshot.png`. The placeholder is intentionally labeled and contains no simulated product data.
