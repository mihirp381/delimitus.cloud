# @delimitus/ssc-files

Keep files from an SSC app: photos, uploads, exports. Zero dependencies; Node 22 or newer.

Ask for file storage in `ssc.toml`:

```toml
[files]
```

The first deploy that asks sets up the cell's data gateway once (a few minutes); after that it is there.

```js
import { get, link, put, remove } from '@delimitus/ssc-files';

await put('photos/cat.png', bytes, { contentType: 'image/png' });
const data = await get('photos/cat.png'); // Uint8Array
const { url } = await link('get', 'photos/cat.png'); // redirect a browser here to download
await remove('photos/cat.png');
```

Each call asks the data gateway for a signed link with the app's own identity, then sends the bytes to Cloud Storage; the app holds no storage credentials. The files belong to this environment alone: production and preview each have their own.

- A file is at most 25 MB; an environment keeps at most 1 GB. `remove` frees space.
- A name is `/`-separated segments of `A-Z a-z 0-9 . _ -`, each starting with a letter or digit, at most 256 characters.
- A link lasts 10 minutes. A download always arrives as an attachment, so a stored HTML file is saved, never shown as a page.
- Upload from the app's server, not from the browser: the bucket allows no browser-direct upload.
- Files are not scanned for viruses.
- When the app is disabled, no new link is given.

Errors are `FilesError` with a `code`: the data gateway's (`FILE_NOT_FOUND`, `FILES_QUOTA_EXCEEDED`, `APP_NOT_ACTIVE`, `VALIDATION_FAILED`, ...), `STORAGE_<status>` when Cloud Storage refused the transfer, or `UNREACHABLE`. The data gateway starts from zero, so a call to it is tried once more on a timeout, a lost connection or a 502, 503 or 504.

The gateway's address comes from the metadata server; `SSC_DATAGW_URL` replaces it. The Python helper is `ssc_app.files`, with the same names (`delete` for `remove`). The contract is `docs/contracts/data-gateway.md#files`.
