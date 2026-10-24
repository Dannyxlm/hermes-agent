# Duty Board development harness

The harness serves `dashboard/dist/index.js` and the real plugin API without a
Hermes dashboard login.  It is safe for local screenshots when pointed at a
synthetic home:

```bash
python tests/fixtures/make_home.py $TMPDIR/duty-board-home
python dev/harness/serve.py --fixture $TMPDIR/duty-board-home --port 9188
```

In a Hermes checkout, build the lightweight SDK served by the harness with
esbuild (React remains external to match the dashboard runtime):

```bash
npx esbuild dev/harness/sdk-entry.js --bundle --format=iife --outfile=dev/harness/sdk.js
```

The harness accepts `--fixture` and sets `HERMES_HOME` before importing the API.
