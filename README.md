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
OPENAI_MODEL=gpt-5.5
DOCSTORE_BACKEND=json
```

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

If you are actively editing code and want auto-reload, use:

```powershell
.\run.ps1 -Reload
```

If you want the older lexical fallback instead, set `DOCSTORE_BACKEND=json`.

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
- Supported direct file types are `.txt`, `.md`, `.rst`, `.csv`, `.html`, `.log`, text-based `.pdf`, Word `.docx`/`.docm`, Excel `.xlsx`/`.xlsm`/`.xltx`/`.xltm`, and structured `.json`
- PDF ingestion preserves page boundaries in the searchable text; encrypted PDFs must be unlocked, and image-only PDFs require OCR before upload
- Structured `.json` uploads can contain one document object or an array of document objects using the same schema as `app/data/sample_documents.json`
- In `semantic` mode, each upload automatically rebuilds the semantic index so the new document is immediately searchable
- In `json` mode, the source corpus is updated without embedding
- Deleting documents from a local `semantic` library also rebuilds the embedding index
- Upload and delete mutations are only available for local `json` and `semantic` backends

Watched Dropbox folder behavior:

- Add watched folders from the document browser by entering a local project root path and an optional subfolder such as `Field Reports`
- If no library folder override is supplied, files import under `Project Folder Name / Monitored Subfolder`
- The app scans watched folders on the configured interval and can also force-sync one folder or all folders from the UI
- The watcher stores its configuration in `WATCHED_FOLDERS_PATH`, defaulting to `app/data/watched_folders.json`
- The scheduler wakes every `WATCHED_FOLDER_POLL_SECONDS`, defaulting to `60`
- Already embedded files are skipped by watched-folder upload key
- Changed files are updated in place and only changed/new documents are re-embedded
- Watched-folder upload keys are scoped by watcher and relative file path so repeated Dropbox project structures do not overwrite each other

Scope behavior:

- If no folders or documents are selected, the assistant can use the full indexed library
- If folders are selected, all documents in those folders are eligible
- If individual documents are selected, those specific documents are eligible
- The active retrieval scope is the union of selected folders and selected documents

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
- There is no authentication or RBAC yet
- The UI is intentionally lightweight
- The local semantic index uses brute-force vector scanning in SQLite, not a dedicated vector database
- The `json` backend is still lexical and exists as a fallback/debug mode

## Recommended next steps

1. Add SSO and per-user audit logging.
2. Move semantic indexing and retrieval into your internal datastore service.
3. Add a background ingestion pipeline for processed documents.
4. Add role-based tool access for business actions.
5. Add evals around answer quality and citation accuracy.
