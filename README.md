# Jenny

This project is a small-business MVP for a ChatGPT-style internal application backed by a private document datastore. The current scaffold uses:

- FastAPI for the application server
- OpenAI Responses API for chat, reasoning, and tool use
- OpenAI Embeddings API for semantic retrieval
- Server-side tools for document search and document retrieval
- A lightweight browser UI for internal users
- A local semantic index backed by SQLite plus a JSON source corpus
- A document browser and context-scope selector in the UI
- In-browser document upload for the local corpus

## What is included

- A chat endpoint at `/api/chat`
- A private tool layer with `search_documents` and `get_document`
- In-memory conversation sessions
- A local sample document corpus in `app/data/sample_documents.json`
- A semantic index builder at `python -m app.build_semantic_index`
- A document library API at `/api/documents` and `/api/documents/{document_id}`
- A document upload API at `/api/documents/upload`
- A document delete API at `/api/documents/delete`
- A switchable datastore adapter:
  - `semantic` for local semantic retrieval
  - `json` for local lexical fallback
  - `http` for a team-server API

## Architecture

The model never talks directly to your datastore. Instead, the app exposes narrow server-side tools:

1. The user sends a message to the FastAPI backend.
2. The backend calls the OpenAI Responses API with internal tool definitions.
3. If the model wants internal knowledge, it calls `search_documents` or `get_document`.
4. The backend executes that tool against your datastore adapter.
5. Tool results are returned to the model.
6. The final answer comes back to the UI along with document citations and a tool trace.

This is the right first step for a small business because it keeps the private data boundary under your control while still giving you a ChatGPT-like product surface.

## Local setup

1. Create a virtual environment.
2. Install dependencies:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and set at least:

```text
OPENAI_API_KEY=your_key_here
OPENAI_STANDARD_MODEL=gpt-5.6-luna
OPENAI_MAXIMUM_MODEL=gpt-5.6-terra
DOCSTORE_BACKEND=json
```

The fixed reasoning control in the chat window uses Luna with medium effort for
**Standard reasoning** and Terra with maximum effort for **Maximum reasoning**.
The selected tier is remembered independently for each conversation.

4. Build the semantic index:

```powershell
python -m app.build_semantic_index
```

5. Run the app:

```powershell
.\run.ps1
```

6. Open `http://127.0.0.1:8000`

For normal use, keep reload off. On Windows, file watching can be dramatically slower, especially when the project contains `.venv`, data files, or large local folders.

### Windows system tray

For normal Windows desktop use, double-click `AskJenny.exe` instead of keeping a
terminal window open. It is a console-free Windows launcher with the custom
**J** application icon. The launcher starts the tray application, which starts
the server in the background and shows its current state in the notification
area.

Right-click the **Ask Jenny** tray icon to:

- Open the application
- Start, stop, or restart the server
- Open the server log
- Exit the tray application and stop the server

The tray uses the custom teal **J** mark when the server and OpenAI connection
are healthy and switches to a warning icon when the server detects an OpenAI
network problem. The same **J** mark appears in the web application and browser
tab.
Double-clicking the icon opens the application. Windows may initially place the
icon in the notification-area overflow menu; drag it onto the taskbar to keep it
permanently visible.

If Ask Jenny is already running, double-clicking `AskJenny.exe` checks for an
open Ask Jenny browser tab. It opens the application in the default browser only
when no live application tab is detected, avoiding duplicate tabs.

To rebuild the executable after changing its icon, metadata, or launcher code:

```powershell
.\build-askjenny.ps1
```

`tray.cmd` remains available as a script-only fallback.

If you are actively editing code and want auto-reload, use:

```powershell
.\run.ps1 -Reload
```

If you want the older lexical fallback instead, set `DOCSTORE_BACKEND=json`.

### User accounts

Use the account badge in the top-right corner to sign in or create an account.
Accounts use a unique username rather than requiring an unverified email
address. Self-service sign-up always creates a **Member** account; the browser
does not offer role or permission assignment. Passwords are salted and hashed,
and signed-in sessions use an HttpOnly cookie.

Saved conversations are private to their owner. Existing conversations without
ownership metadata are assigned to the default Administrator during migration.
New conversations are attached to the signed-in user, and list, load, update,
and delete operations are filtered by that owner.

Saved conversations use normalized SQLite storage at
`app/data/saved_conversations.sqlite`. Metadata and messages are updated in
atomic WAL transactions, while images and generated files are stored as raw,
content-addressed BLOBs so identical legacy/current document fields occupy one
copy. Unchanged message prefixes are not rewritten when only conversation
settings or titles change. On the first startup after upgrading, the legacy
`saved_conversations.json` file is imported transactionally and left untouched
as a recovery copy. Configure the active database with
`SAVED_CONVERSATIONS_DATABASE_PATH`; `SAVED_CONVERSATIONS_PATH` identifies only
the legacy migration source.

Users can attach up to five JPEG, PNG, WebP, or GIF images to a chat message by
choosing files, pasting from the clipboard, or dragging images onto the
composer. Each image can be up to 8 MB. Attachments are sent as multimodal model
input and saved with the private conversation. On later turns, the image name
and the assistant's prior interpretation remain in context, but the large
base64 image payload is not resent automatically.

Saved conversations keep their complete private transcript. Model requests use
a bounded recent window plus up to four locally ranked excerpts from older
turns, allowing earlier decisions and preferences to be recalled without an
extra OpenAI completion or embedding call. Historical excerpts are selected
only from the authenticated user's current conversation, are treated as quoted
content rather than system instructions, and are capped at 4,000 characters by
default. Configure or disable this behavior with `CHAT_MEMORY_ENABLED`,
`CHAT_MEMORY_MAX_CHARS`, and `CHAT_MEMORY_MAX_TURNS`; recent verbatim context is
controlled by `CHAT_HISTORY_MAX_MESSAGES` and `CHAT_HISTORY_MAX_CHARS`.
Before every OpenAI model round, the complete request—including system
instructions, enabled tool schemas, recent history, locally recalled memory,
current-turn tool output, encrypted reasoning continuation, and bounded-detail
images—is checked against a deterministic input envelope. Optional old turns
are removed first; required current-turn tool/reasoning state is never silently
truncated. If required state alone exceeds the envelope, the turn stops before
incurring another API call. Configure the envelope with
`CHAT_MAX_INPUT_BUDGET` and the per-image charge with
`CHAT_IMAGE_BUDGET_UNITS`.

In **Context: Global**, the agent can find an official datasheet, manual,
specification, or other public PDF and attach the original file to the private
conversation. Retrieved source PDFs are not OCRed, embedded, synchronized, or
added to the document library. Retrieval accepts direct public HTTPS links only,
blocks local/private network destinations (including redirects), verifies the
PDF signature, and preserves the downloaded bytes. The default limit is 20 MB;
configure `SOURCE_DOWNLOAD_TIMEOUT_SECONDS` and `SOURCE_DOWNLOAD_MAX_BYTES` if
needed.

Internet-enabled chat requests are bounded so a failed search or slow download
cannot run indefinitely. By default, a response has a 120-second overall budget,
an individual OpenAI call has a 60-second ceiling with automatic retries
disabled, the agent can perform at most three model/tool rounds, and it can make
only one source-PDF download attempt. The composer exposes **Cancel response**
while a request is active. Configure these limits with
`CHAT_REQUEST_TIMEOUT_SECONDS`, `OPENAI_REQUEST_TIMEOUT_SECONDS`,
`CHAT_MAX_TOOL_ROUNDS`, and `SOURCE_DOWNLOAD_MAX_ATTEMPTS`.

Requests containing two to eight product model numbers and the word
“datasheet” use the bounded batch-retrieval path. Each model is searched
independently and in parallel, successful original PDFs are attached as
separate private-conversation downloads, duplicates are removed, and a failure
for one product does not discard the other successful files. The response
shows an elapsed-time retrieval status while the batch is active.

Administrator bootstrap credentials are deployment secrets and are
intentionally omitted from this repository's documentation and example
configuration. Set `DEFAULT_ADMIN_USERNAME`, `DEFAULT_ADMIN_DISPLAY_NAME`, and
`DEFAULT_ADMIN_PASSWORD` only in the untracked private `.env` before deployment,
and change the bootstrap password after first sign-in. Never publish or commit
the resulting login. Account records and server-side session tokens are stored
in `app/data/application.sqlite` by default; change the location with
`APPLICATION_DATABASE_PATH`.

## Portable Docker setup

Docker packages the application and its OCR runtime together. Copy `.env.example` to `.env`, add your OpenAI key, and run:

```powershell
docker compose up --build
```

Open `http://127.0.0.1:8000`. The default image installs Tesseract and its English language data automatically. Application data and generated files persist in the `app-data` and `app-outputs` Docker volumes.

To build the pip-installable RapidOCR variant instead, set this in `.env` before building:

```text
PDF_OCR_ENGINE=rapidocr
```

Rebuild after changing engines:

```powershell
docker compose build --no-cache
docker compose up
```

## Wiring this to your team server

## Local semantic retrieval

The local semantic mode reads source documents from `DOCSTORE_JSON_PATH`, chunks them, creates two embeddings per chunk, and writes a SQLite index to `SEMANTIC_INDEX_PATH`. The app uses the smaller search embedding profile for normal library retrieval and the larger answer embedding profile when the agent is selecting context for answers or generated documents.

Relevant settings:

```text
DOCSTORE_BACKEND=semantic
WATCHED_FOLDERS_PATH=app/data/watched_folders.json
WATCHED_FOLDER_POLL_SECONDS=60
SEMANTIC_INDEX_PATH=app/data/semantic_documents.sqlite
SEMANTIC_SEARCH_EMBEDDING_MODEL=text-embedding-3-small
SEMANTIC_SEARCH_EMBEDDING_DIMENSIONS=
SEMANTIC_ANSWER_EMBEDDING_MODEL=text-embedding-3-large
SEMANTIC_ANSWER_EMBEDDING_DIMENSIONS=
SEMANTIC_CHUNK_SIZE_WORDS=220
SEMANTIC_CHUNK_OVERLAP_WORDS=40
SEMANTIC_EMBEDDING_BATCH_SIZE=32
```

`SEMANTIC_EMBEDDING_MODEL` and `SEMANTIC_EMBEDDING_DIMENSIONS` are still accepted as backward-compatible aliases for the search embedding profile.

Rebuild the index whenever the source JSON changes. If upgrading from the older single-embedding index, rebuild once so existing chunks receive both search and answer embeddings:

```powershell
python -m app.build_semantic_index
```

If the app reports that the semantic index schema is outdated, rebuild the index with the same command.

## Document browser and scope control

The UI now lets users:

- Browse the indexed documents that are available to retrieval
- Preview the embedded source text and metadata for a document
- Upload new text, PDF, Word, Excel, and structured JSON source documents into the local corpus
- Add watched local folders, such as Dropbox project subfolders, that sync into the embedded library
- Select and delete embedded documents from the local corpus
- Restrict chat retrieval to selected folders and/or selected document IDs

Upload behavior:

- The upload form lives inside the document browser
- Supported direct file types are `.txt`, `.md`, `.rst`, `.csv`, `.html`, `.log`, `.pdf`, Word `.docx`/`.docm`, Excel `.xlsx`/`.xlsm`/`.xltx`/`.xltm`, and structured `.json`
- The document generator can create downloadable TXT, editable DOCX, polished PDF, and XLSX files from the selected internal library scope. Generated PDFs include styled headings, bullets, tables, source references, headers, footers, and page numbers.
- PDF ingestion preserves page boundaries and automatically OCRs scanned or image-only pages; encrypted PDFs must be unlocked before upload
- Structured `.json` uploads can contain one document object or an array of document objects using the same schema as `app/data/sample_documents.json`
- In `semantic` mode, each upload automatically rebuilds the semantic index so the new document is immediately searchable
- In `json` mode, the source corpus is updated without embedding
- Deleting documents from a local `semantic` library also rebuilds the embedding index
- Upload and delete mutations are only available for local `json` and `semantic` backends

### PDF OCR setup

PDF rendering is installed through `requirements.txt`. OCR providers are interchangeable and selected with `PDF_OCR_ENGINE`.

PDF ingestion uses a hybrid image-understanding pipeline:

- Native PDF text is preserved.
- RapidOCR reads text from scanned pages and from embedded images on otherwise text-rich pages.
- Duplicate OCR lines are removed before indexing.
- Bounded GPT-5.6 Luna vision batches describe charts, diagrams, photographs, callouts, legends, equipment, and spatial relationships that OCR cannot interpret.
- Vision failures are logged and do not discard otherwise extractable document text.

Vision analysis is enabled by default when `OPENAI_API_KEY` is configured. It prioritizes image-bearing, scanned, and graphics-heavy pages and analyzes at most 12 pages per PDF by default. Configure the limits with `PDF_VISION_MAX_PAGES`, `PDF_VISION_BATCH_SIZE`, `PDF_VISION_DPI`, and `PDF_VISION_MAX_DIMENSION`, or disable external image analysis with `PDF_VISION_ENABLED=false`. Mixed-page image OCR can be controlled independently with `PDF_IMAGE_OCR_ENABLED` and `PDF_IMAGE_OCR_MAX_PAGES`.

Semantic document deletion uses an atomic in-place SQLite transaction. Deletion-only operations remove the selected document and chunk rows without copying the entire index or regenerating unchanged embeddings. The library UI streams real deletion phases, percentage completion, and elapsed time while the operation runs.

The default `tesseract` provider requires the Tesseract command-line program and the language data used by your documents. Docker installs both automatically. For a native installation, install Tesseract on the application server, make sure `tesseract` is on `PATH`, and restart the app. If it is installed elsewhere, set `PDF_OCR_TESSERACT_CMD` to the full executable path.

The optional `rapidocr` provider installs through pip and includes its default English/Chinese small models. For a native installation, run:

```powershell
pip install -r requirements-rapidocr.txt
```

Then set `PDF_OCR_ENGINE=rapidocr`. RapidOCR does not use `PDF_OCR_LANGUAGE` or `PDF_OCR_TESSERACT_CMD`; use the Tesseract provider when you need the configured Tesseract language packs.

OCR is applied per page only when the page has little or no embedded text, which preserves good native text while supporting mixed digital/scanned PDFs. Configure it with:

```text
PDF_OCR_ENABLED=true
PDF_OCR_ENGINE=tesseract
PDF_OCR_LANGUAGE=eng
PDF_OCR_DPI=300
PDF_OCR_MIN_NATIVE_TEXT_CHARS=40
PDF_OCR_TIMEOUT_SECONDS=60
PDF_OCR_TESSERACT_CMD=tesseract
PDF_MAX_PAGES=500
```

Use Tesseract language codes such as `eng` or a combination such as `eng+spa`. Set `PDF_OCR_ENABLED=false` only if this deployment must reject image-only pages instead of running OCR.

`PDF_MAX_PAGES` bounds the work performed by a single upload. Increase it deliberately for unusually large document sets.

The local startup script checks the configured OCR runtime and warns when it is unavailable. `/api/health` also reports the selected provider and whether its dependencies are present.

Watched Dropbox folder behavior:

- Add watched folders from the document browser by entering a local project root path and an optional subfolder such as `Field Reports`
- If no library folder override is supplied, files import under `Project Folder Name / Monitored Subfolder`
- Project paths under a numbered `Projects` folder automatically populate structured workflow tags from the Dropbox hierarchy
- The app scans watched folders on the configured interval and can also force-sync one folder or all folders from the UI
- If a numbered Dropbox project folder is renamed, a missing watched path is rebound automatically only when exactly one sibling project with the same project number contains the expected subfolder; ambiguous matches remain errors
- The watcher stores its configuration in `WATCHED_FOLDERS_PATH`, defaulting to `app/data/watched_folders.json`
- The scheduler wakes every `WATCHED_FOLDER_POLL_SECONDS`, defaulting to `60`
- The Document Library's **Synchronized paths** menu lists every monitored source path, its mapped library folder, schedule, last result, and management actions
- Synchronized-folder properties include **Open source location**, which resolves the saved watcher path and opens it in the operating system's file manager
- Already embedded files are skipped by watched-folder upload key
- Changed files are updated in place and only changed/new documents are re-embedded
- Online-only Dropbox/OneDrive placeholders require the desktop sync client to be running and the files to be available offline; unavailable files are reported individually without blocking readable files in the same folder
- Watched-folder upload keys are scoped by watcher and relative file path so repeated Dropbox project structures do not overwrite each other
- Deleting a synchronized library folder also removes every watcher mapped to that folder or its descendants, preventing the folder from returning on the next scan; source files on disk are never deleted

### Watched-folder path tags

The watcher understands the team's numbered Dropbox project layout. For example:

```text
\Vasquez Integrators Dropbox\01. Project Delivery\00. Projects\43. PANYNJ - EWR Innomotics VMSS\Working Moh\Project Notes\meeting.md
```

produces:

```text
workflow:project
project-number:43
project:PANYNJ - EWR Innomotics VMSS
client:PANYNJ
site:EWR
owner:Moh
workstream:Project Notes
```

Tags configured on the watcher are also treated as watcher-managed auto tags. Tags added manually to an individual library document are preserved when the source file changes or resyncs. If the path or watcher tags change, obsolete watcher-managed tags are replaced. Existing synchronized documents are backfilled on their next sync even when their file contents have not changed.

Scope behavior:

- In Global context, no folders or documents are selected by default, so internal library retrieval is not exposed
- In Internal context, if no folders or documents are selected, the assistant can use the full indexed library
- If folders are selected, all documents in those folders are eligible
- If individual documents are selected, those specific documents are eligible
- The active retrieval scope is the union of selected folders and selected documents
- Each chat remembers its own Context: Internal/Context: Global mode and embedded-library scope; changing either setting keeps the current chat open, saved chats restore those choices, and new chats start in Global context with no internal scope

## Wiring this to your team server

If your team server already has its own semantic or hybrid retrieval service, the app can call that instead:

1. Set `DOCSTORE_BACKEND=http`
2. Point `DOCSTORE_BASE_URL` at your internal service
3. Make your service implement:

- `POST /documents/search`
- `GET /documents/{document_id}`

Expected search request body:

```json
{
  "query": "invoice dispute policy",
  "limit": 5
}
```

Expected search response body:

```json
{
  "results": [
    {
      "document_id": "FIN-014",
      "title": "Invoice Dispute Process",
      "category": "finance",
      "summary": "How the team handles invoice disputes.",
      "excerpt": "If a customer disputes an invoice, create a case in the billing queue...",
      "score": 12.4,
      "source_url": "https://intranet.local/docs/fin-014"
    }
  ]
}
```

Expected document response body:

```json
{
  "document_id": "FIN-014",
  "title": "Invoice Dispute Process",
  "category": "finance",
  "tags": ["billing", "accounts-receivable"],
  "summary": "How the team handles invoice disputes.",
  "text": "Full processed document text here.",
  "source_url": "https://intranet.local/docs/fin-014",
  "updated_at": "2026-05-18"
}
```

## Important MVP limits

- Sessions are in memory only
- Local accounts authenticate with passwords and own private conversations, but shared-library role enforcement is not connected yet
- The UI is intentionally lightweight
- The local semantic index uses brute-force vector scanning in SQLite, not a dedicated vector database
- The `json` backend is still lexical and exists as a fallback/debug mode

## Recommended next steps

1. Enforce user roles for shared-library administration and optionally connect team SSO.
2. Move semantic indexing and retrieval into your internal datastore service.
3. Add a background ingestion pipeline for processed documents.
4. Add role-based tool access for business actions.
5. Add evals around answer quality and citation accuracy.
