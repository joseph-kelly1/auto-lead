# Development guide

## Public demo

The checked-in `web/public/parcels.parquet` and `parcels.pmtiles` contain only
synthetic data. `dataset.json` identifies the dataset as synthetic. The viewer
shows a demo notice, and fictional parcels do not trigger provider lookups or
property-search links.

The demo generator uses invented inputs and the shared project cost and scoring
functions. It does not read assessor records or copy production parcel geometry.
Its schema fixture preserves the viewer's 146-column data contract.

To regenerate the demo, install Python 3.11 or newer, [uv](https://docs.astral.sh/uv/),
and [tippecanoe](https://github.com/felt/tippecanoe):

```sh
cd pipeline
uv sync --locked
uv run demo.py
```

Commit the two generated demo artifacts and their `dataset.json` together when
changing the demo. Normal web builds validate these files and never copy private
pipeline outputs automatically.

## Pipeline checks

```sh
cd pipeline
uv sync --locked
uv run model.py
uv run build.py --selfcheck
```

These checks cover score behavior, missing inputs, feasibility gates, pro forma
calculations, and a small synthetic comparable-sales example. They do not require
source downloads.

From `web/`, run `npm ci`, `npm run check`, and `npm run build` for application
validation. Font fetching during the build requires access to Google Fonts.

## Build with real source records

The production pipeline downloads MassGIS parcel archives and constraint layers.
It writes private local outputs to `pipeline/artifacts/` and caches downloads in
`pipeline/data/raw/`. A full build can download hundreds of megabytes or more.
Both directories are ignored by Git.

```sh
cd pipeline
uv sync --locked
uv run build.py
uv run fetch_zoning_assignments.py
uv run build.py
```

The first build supplies parcel centroids for municipal zoning joins. Refreshing
zoning assignments and rebuilding applies official district matches. The local
`zoning_assignments.csv` is ignored; municipal standards and their source URLs
remain in `zoning_minimums.csv`.

Optional `listings.csv` and `permits.csv` inputs are also ignored. Their checked-in
`.example.csv` files document the expected headers. Read [the model notes](model.md)
for source coverage, assumptions, and missing-data behavior.

Historical evaluation uses cached current artifacts and downloads older assessor
vintages as needed:

```sh
uv run calibrate.py
uv run calibrate.py --pairs 5 --target viable --fit
```

Fitting requires enough training successes. A time-ordered split does not remove
all possible sources of leakage; see the validation discussion before interpreting
historical metrics.

## View private outputs locally

After generating both real artifacts, explicitly stage them into the viewer:

```sh
cd web
npm run stage -- --private
npm run dev
```

This replaces the synthetic files in `web/public/` with real property records.
Keep this working copy private. Restore the demo with
`cd ../pipeline && uv run demo.py` before committing or publishing. An ordinary
`npm run build` does not refresh private data and rejects staged private artifacts.
A deliberate private production build requires
`ALLOW_PRIVATE_ARTIFACTS=1 npm run build`.

For external artifact hosting, set `NEXT_PUBLIC_ARTIFACT_BASE` to the artifact
origin. It must support HTTP range requests and appropriate browser CORS access.
Leave the variable blank for same-origin files in `public/`.

## Authentication and market integrations

`APP_ACCESS_KEY` must contain at least 24 random characters. Login creates a signed,
HttpOnly session cookie. The shared-key design supports a small private research
workflow.

Optional provider credentials are stored server-side in a private Vercel Blob
store, encrypted with a key derived from `APP_ACCESS_KEY`. Rotating the access key
also requires re-entering provider credentials. Local Blob access can use
`BLOB_READ_WRITE_TOKEN`; linked deployments may use store OIDC credentials.

The credentials dialog accepts authorized Bridge Zillow and MLS server tokens.
Market requests happen on demand and do not feed the offline score. The synthetic
demo does not send its invented addresses to providers.

## Deploy the viewer

1. Import the repository into Vercel and set the root directory to `web`.
2. Configure `APP_ACCESS_KEY` for each intended deployment environment.
3. Use the detected Next.js build settings. The bundled synthetic artifacts work
   without a pipeline build or Blob store.
4. For a private real-data deployment, configure your artifact origin explicitly.
5. If using provider credentials, connect a private Blob store to the project.

Public and preview deployments should use the synthetic demo unless their data
access has been configured deliberately. Authentication of the viewer does not
make committed data private: Git history and deployed static files must be
considered separately.

## Data sources

The real pipeline uses [MassGIS Level 3 parcels](https://www.mass.gov/info-details/massgis-data-standardized-assessors-parcels),
MassGIS environmental and historic layers, and municipal zoning GIS services.
Exact endpoints live in `build.py`, `fetch_zoning_assignments.py`, and source URL
columns in `zoning_minimums.csv`. Source records are downloaded at runtime rather
than redistributed in the public demo.

The generated dataset contains no real owner names, mailing addresses, property
addresses, or municipal parcel identifiers. Local environment files, notes exports,
and private input files should stay outside Git.
