const modeCopy = {
  internal: {
    description: "Prioritizes internal documents and avoids filling gaps with general knowledge.",
    composerNote: "Internal facts should come from the datastore, not memory.",
    intro:
      "Ask about your internal documents or any general question. In this mode, company-specific answers should stay grounded in the datastore first.",
  },
  broader: {
    description:
      "Still checks internal documents, and can search the public web for current broader context.",
    composerNote:
      "Uses internal docs when relevant and visibly cites public web sources when it searches.",
    intro:
      "Ask for internal answers, current public information, or broader explainers. The assistant can combine internal sources with cited web research when helpful.",
  },
};

const CHAT_PREFERENCES_STORAGE_KEY = "business-knowledge-chat-preferences-v1";
const MAX_STORED_CHAT_PREFERENCES = 100;
const conversationPreferenceSyncTimers = new Map();

const state = {
  conversationId: crypto.randomUUID(),
  sourceMode: "internal",
  sending: false,
  responseIndicatorNode: null,
  messages: [],
  memory: {
    conversations: [],
    loaded: false,
    loadError: null,
    searchQuery: "",
    saveInFlight: false,
    renameInFlightId: null,
    deleteInFlightId: null,
    contextMenu: {
      open: false,
      conversationId: null,
      x: 0,
      y: 0,
    },
  },
  generation: {
    inFlight: false,
  },
  library: {
    backend: null,
    totalDocuments: 0,
    totalChunks: null,
    folders: [],
    documents: [],
    loaded: false,
    loadError: null,
    activeFolderId: null,
    searchQuery: "",
    collapsedFolderIds: [],
    collapseFoldersOnLoad: false,
    dragPayload: null,
    previewDocumentId: null,
    previewCache: {},
    uploadInFlight: false,
    metadataUpdateInFlight: false,
    deleteInFlight: false,
    deleteSelectionIds: [],
    editorDocumentId: null,
    editorDirty: false,
    editorDismissed: false,
    watchFolders: [],
    watchFoldersLoaded: false,
    watchFoldersLoadError: null,
    watchFolderInFlight: false,
    inlineRenameFolderId: null,
    inlineRenameDraft: "",
    inlineRenameOriginalValue: "",
    inlineRenameInFlight: false,
    contextMenu: {
      open: false,
      targetType: null,
      targetId: null,
      label: "",
      x: 0,
      y: 0,
    },
    appliedContext: {
      folderIds: [],
      documentIds: [],
    },
    draftContext: {
      folderIds: [],
      documentIds: [],
    },
  },
};

const messageList = document.getElementById("messageList");
const composerForm = document.getElementById("composerForm");
const messageInput = document.getElementById("messageInput");
const sendButton = document.getElementById("sendButton");
const newConversationButton = document.getElementById("newConversationButton");
const saveConversationButton = document.getElementById("saveConversationButton");
const conversationSearchInput = document.getElementById("conversationSearchInput");
const savedConversationList = document.getElementById("savedConversationList");
const conversationMemoryStatus = document.getElementById("conversationMemoryStatus");
const savedConversationContextMenu = document.getElementById("savedConversationContextMenu");
const savedConversationRenameButton = document.getElementById("savedConversationRenameButton");
const savedConversationDeleteButton = document.getElementById("savedConversationDeleteButton");
const statusPill = document.getElementById("statusPill");
const messageTemplate = document.getElementById("messageTemplate");
const composerNote = document.getElementById("composerNote");
const modeDescription = document.getElementById("modeDescription");
const modeButtons = Array.from(document.querySelectorAll("[data-source-mode]"));
const contextSummary = document.getElementById("contextSummary");
const contextChipList = document.getElementById("contextChipList");
const openLibraryButton = document.getElementById("openLibraryButton");
const clearContextButton = document.getElementById("clearContextButton");
const documentGenerationForm = document.getElementById("documentGenerationForm");
const documentGenerationTitleInput = document.getElementById("documentGenerationTitleInput");
const documentGenerationFormatSelect = document.getElementById("documentGenerationFormatSelect");
const documentGenerationInstructionsInput = document.getElementById("documentGenerationInstructionsInput");
const documentGenerationStatus = document.getElementById("documentGenerationStatus");
const generateDocumentButton = document.getElementById("generateDocumentButton");
const documentBrowser = document.getElementById("documentBrowser");
const closeBrowserButton = document.getElementById("closeBrowserButton");
const browserStats = document.getElementById("browserStats");
const scopeInventorySummary = document.getElementById("scopeInventorySummary");
const scopeAppliedSummary = document.getElementById("scopeAppliedSummary");
const scopeDraftSummary = document.getElementById("scopeDraftSummary");
const scopeIncludedList = document.getElementById("scopeIncludedList");
const scopeExcludedList = document.getElementById("scopeExcludedList");
const folderTreeList = document.getElementById("folderTreeList");
const folderTreeSurface = folderTreeList.parentElement;
const applyContextButton = document.getElementById("applyContextButton");
const browserUseAllButton = document.getElementById("browserUseAllButton");
const deleteSelectedButton = document.getElementById("deleteSelectedButton");
const explorerRootButton = document.getElementById("explorerRootButton");
const explorerExpandAllButton = document.getElementById("explorerExpandAllButton");
const explorerCollapseAllButton = document.getElementById("explorerCollapseAllButton");
const explorerRevealSelectionButton = document.getElementById("explorerRevealSelectionButton");
const librarySyncAllButton = document.getElementById("librarySyncAllButton");
const libraryBreadcrumbs = document.getElementById("libraryBreadcrumbs");
const librarySearchInput = document.getElementById("librarySearchInput");
const documentFileList = document.getElementById("documentFileList");
const previewEmpty = document.getElementById("previewEmpty");
const previewCard = document.getElementById("previewCard");
const previewBadges = document.getElementById("previewBadges");
const previewTitle = document.getElementById("previewTitle");
const previewSummary = document.getElementById("previewSummary");
const previewMeta = document.getElementById("previewMeta");
const previewText = document.getElementById("previewText");
const documentEditorEmpty = document.getElementById("documentEditorEmpty");
const documentEditorForm = document.getElementById("documentEditorForm");
const documentEditorId = document.getElementById("documentEditorId");
const documentEditorTitleInput = document.getElementById("documentEditorTitleInput");
const documentEditorCategoryInput = document.getElementById("documentEditorCategoryInput");
const documentEditorFolderInput = document.getElementById("documentEditorFolderInput");
const documentEditorTagsInput = document.getElementById("documentEditorTagsInput");
const documentEditorStatus = document.getElementById("documentEditorStatus");
const saveDocumentChangesButton = document.getElementById("saveDocumentChangesButton");
const closeDocumentEditorButton = document.getElementById("closeDocumentEditorButton");
const folderPropertiesCard = document.getElementById("folderPropertiesCard");
const folderPropertiesTitle = document.getElementById("folderPropertiesTitle");
const folderPropertiesKind = document.getElementById("folderPropertiesKind");
const folderPropertiesPath = document.getElementById("folderPropertiesPath");
const folderPropertiesAliasRow = document.getElementById("folderPropertiesAliasRow");
const folderPropertiesAliasInput = document.getElementById("folderPropertiesAliasInput");
const folderPropertiesSourceRow = document.getElementById("folderPropertiesSourceRow");
const folderPropertiesSource = document.getElementById("folderPropertiesSource");
const folderPropertiesDocumentCount = document.getElementById("folderPropertiesDocumentCount");
const folderPropertiesCategory = document.getElementById("folderPropertiesCategory");
const folderPropertiesScheduleRow = document.getElementById("folderPropertiesScheduleRow");
const folderPropertiesSchedule = document.getElementById("folderPropertiesSchedule");
const folderPropertiesWatchSettings = document.getElementById("folderPropertiesWatchSettings");
const folderPropertiesIntervalInput = document.getElementById("folderPropertiesIntervalInput");
const folderPropertiesCategoryInput = document.getElementById("folderPropertiesCategoryInput");
const folderPropertiesTagsInput = document.getElementById("folderPropertiesTagsInput");
const folderPropertiesRecursiveInput = document.getElementById("folderPropertiesRecursiveInput");
const folderPropertiesEnabledInput = document.getElementById("folderPropertiesEnabledInput");
const folderPropertiesTags = document.getElementById("folderPropertiesTags");
const renameSelectedFolderButton = document.getElementById("renameSelectedFolderButton");
const syncSelectedFolderButton = document.getElementById("syncSelectedFolderButton");
const closeFolderPropertiesButton = document.getElementById("closeFolderPropertiesButton");
const deleteSelectionSummary = document.getElementById("deleteSelectionSummary");
const deleteChipList = document.getElementById("deleteChipList");
const libraryActionStatus = document.getElementById("libraryActionStatus");
const uploadForm = document.getElementById("uploadForm");
const uploadFileInput = document.getElementById("uploadFileInput");
const uploadDirectoryInput = document.getElementById("uploadDirectoryInput");
const uploadCategoryInput = document.getElementById("uploadCategoryInput");
const uploadFolderInput = document.getElementById("uploadFolderInput");
const uploadTitleInput = document.getElementById("uploadTitleInput");
const uploadTagsInput = document.getElementById("uploadTagsInput");
const uploadStatus = document.getElementById("uploadStatus");
const uploadButton = document.getElementById("uploadButton");
const selectUploadFilesButton = document.getElementById("selectUploadFilesButton");
const selectUploadDirectoryButton = document.getElementById("selectUploadDirectoryButton");
const syncFolderActionButton = document.getElementById("syncFolderActionButton");
const watchCard = document.getElementById("watchCard");
const closeWatchFolderButton = document.getElementById("closeWatchFolderButton");
const watchFolderForm = document.getElementById("watchFolderForm");
const watchRootPathInput = document.getElementById("watchRootPathInput");
const browseWatchRootPathButton = document.getElementById("browseWatchRootPathButton");
const watchAliasInput = document.getElementById("watchAliasInput");
const watchSubfolderInput = document.getElementById("watchSubfolderInput");
const watchIntervalInput = document.getElementById("watchIntervalInput");
const watchLibraryFolderInput = document.getElementById("watchLibraryFolderInput");
const watchCategoryInput = document.getElementById("watchCategoryInput");
const watchTagsInput = document.getElementById("watchTagsInput");
const watchRecursiveInput = document.getElementById("watchRecursiveInput");
const watchFolderStatus = document.getElementById("watchFolderStatus");
const watchFolderList = document.getElementById("watchFolderList");
const addWatchFolderButton = document.getElementById("addWatchFolderButton");
const syncAllWatchFoldersButton = document.getElementById("syncAllWatchFoldersButton");
const openSynchronizedPathsButton = document.getElementById("openSynchronizedPathsButton");
const synchronizedPathsCount = document.getElementById("synchronizedPathsCount");
const synchronizedPathsMenu = document.getElementById("synchronizedPathsMenu");
const synchronizedPathsSummary = document.getElementById("synchronizedPathsSummary");
const closeSynchronizedPathsButton = document.getElementById("closeSynchronizedPathsButton");
const addSynchronizedPathButton = document.getElementById("addSynchronizedPathButton");
const explorerContextMenu = document.getElementById("explorerContextMenu");
const explorerContextNewFolderButton = document.getElementById("explorerContextNewFolderButton");
const explorerContextRenameButton = document.getElementById("explorerContextRenameButton");
const explorerContextDeleteButton = document.getElementById("explorerContextDeleteButton");

const SUPPORTED_UPLOAD_EXTENSIONS = new Set([
  ".csv",
  ".docm",
  ".docx",
  ".htm",
  ".html",
  ".json",
  ".log",
  ".markdown",
  ".md",
  ".pdf",
  ".rst",
  ".text",
  ".txt",
  ".xlsm",
  ".xlsx",
  ".xltm",
  ".xltx",
]);
const BINARY_UPLOAD_EXTENSIONS = new Set([
  ".docm",
  ".docx",
  ".pdf",
  ".xlsm",
  ".xlsx",
  ".xltm",
  ".xltx",
]);
const AUTO_TAG_FOLDER_PREFIX = "folder:";
const AUTO_TAG_PATH_PREFIX = "folder-path:";

function normalizeItems(items) {
  return Array.from(new Set(items)).sort((left, right) => left.localeCompare(right));
}

function normalizeTagItems(rawTags) {
  const items = Array.isArray(rawTags) ? rawTags : String(rawTags || "").split(",");
  const normalized = [];
  const seen = new Set();

  items.forEach((item) => {
    const tag = String(item).trim();
    if (!tag) {
      return;
    }
    const key = tag.toLowerCase();
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
    normalized.push(tag);
  });

  return normalized;
}

function buildFolderAutoTags(folderId) {
  const normalizedFolderId = normalizeFolderPath(folderId || "");
  if (!normalizedFolderId) {
    return [];
  }

  const autoTags = [];
  const seen = new Set();
  const lineageParts = [];

  normalizedFolderId.split("/").forEach((segment) => {
    const normalizedSegment = String(segment || "").trim();
    if (!normalizedSegment) {
      return;
    }
    lineageParts.push(normalizedSegment);
    [
      `${AUTO_TAG_FOLDER_PREFIX}${normalizedSegment}`,
      `${AUTO_TAG_PATH_PREFIX}${lineageParts.join("/")}`,
    ].forEach((candidate) => {
      const key = candidate.toLowerCase();
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
      autoTags.push(candidate);
    });
  });

  return autoTags;
}

function stripAutoTagsForFolder(rawTags, folderId) {
  const autoTagKeys = new Set(buildFolderAutoTags(folderId).map((tag) => tag.toLowerCase()));
  return normalizeTagItems(rawTags).filter((tag) => !autoTagKeys.has(tag.toLowerCase()));
}

function getUploadFileExtension(filename) {
  const normalizedName = String(filename || "").trim().toLowerCase();
  const dotIndex = normalizedName.lastIndexOf(".");
  if (dotIndex < 0) {
    return "";
  }
  return normalizedName.slice(dotIndex);
}

function isSupportedUploadFile(fileLike) {
  return SUPPORTED_UPLOAD_EXTENSIONS.has(getUploadFileExtension(fileLike && fileLike.name));
}

function isBinaryUploadFile(fileLike) {
  return BINARY_UPLOAD_EXTENSIONS.has(getUploadFileExtension(fileLike && fileLike.name));
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const payload = String(reader.result || "");
      const commaIndex = payload.indexOf(",");
      resolve(commaIndex >= 0 ? payload.slice(commaIndex + 1) : payload);
    };
    const rejectUnreadableFile = () => {
      const filename = file && file.name ? file.name : "file";
      reject(new Error(
        `Failed to read ${filename}. If it is an online-only Dropbox, OneDrive, or other cloud file, ` +
        "start the sync client and make the file available offline before retrying."
      ));
    };
    reader.onerror = rejectUnreadableFile;
    reader.onabort = rejectUnreadableFile;
    reader.readAsDataURL(file);
  });
}

function decodeBase64ToBytes(contentBase64) {
  const binaryString = window.atob(String(contentBase64 || ""));
  const bytes = new Uint8Array(binaryString.length);
  for (let index = 0; index < binaryString.length; index += 1) {
    bytes[index] = binaryString.charCodeAt(index);
  }
  return bytes;
}

function downloadBase64File(filename, mimeType, contentBase64) {
  const blob = new Blob([decodeBase64ToBytes(contentBase64)], {
    type: mimeType || "application/octet-stream",
  });
  const objectUrl = window.URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename || "generated-document";
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => {
    window.URL.revokeObjectURL(objectUrl);
  }, 0);
}

function getErrorMessageFromPayload(payload, fallbackMessage = "Unknown server error") {
  if (!payload) {
    return fallbackMessage;
  }
  if (typeof payload.detail === "string" && payload.detail) {
    return payload.detail;
  }
  if (payload.detail && typeof payload.detail.message === "string" && payload.detail.message) {
    return payload.detail.message;
  }
  if (typeof payload.message === "string" && payload.message) {
    return payload.message;
  }
  return fallbackMessage;
}

function clearUploadSelections({ preserveDirectory = false } = {}) {
  uploadFileInput.value = "";
  if (!preserveDirectory) {
    uploadDirectoryInput.value = "";
  }
}

function buildUploadFolderPath(baseFolder, relativeDirectory) {
  const normalizedBase = String(baseFolder || "").trim();
  const normalizedRelative = normalizeFolderPath(relativeDirectory || "");
  if (normalizedBase && normalizedRelative) {
    return normalizeFolderPath(`${normalizedBase}/${normalizedRelative}`);
  }
  if (normalizedBase) {
    return normalizeFolderPath(normalizedBase);
  }
  if (normalizedRelative) {
    return normalizedRelative;
  }
  return "";
}

async function buildUploadPayloads(files, options = {}) {
  const {
    category,
    folderBase,
    titleOverride,
    tags,
  } = options;
  const supportedFiles = files.filter((file) => isSupportedUploadFile(file));
  if (supportedFiles.length === 0) {
    throw new Error("No supported files were selected.");
  }

  const documents = [];
  const applySharedTitle = supportedFiles.length === 1 ? titleOverride : "";
  for (const file of supportedFiles) {
    const relativePath = String(file.webkitRelativePath || "").replace(/\\/g, "/");
    const relativeParts = relativePath
      ? relativePath.split("/").filter(Boolean)
      : [];
    const relativeDirectory = relativeParts.length > 1 ? relativeParts.slice(0, -1).join("/") : "";
    const isBinaryFile = isBinaryUploadFile(file);
    const contentText = isBinaryFile ? null : await file.text();
    const contentBase64 = isBinaryFile ? await readFileAsBase64(file) : null;
    documents.push({
      filename: file.name,
      content_text: contentText,
      content_base64: contentBase64,
      client_path: relativePath || file.name,
      client_modified_ms: Number.isFinite(file.lastModified) ? file.lastModified : null,
      category: category || null,
      folder: buildUploadFolderPath(folderBase, relativeDirectory) || null,
      title: applySharedTitle || null,
      tags,
    });
  }

  return {
    documents,
    supportedCount: supportedFiles.length,
    skippedCount: files.length - supportedFiles.length,
  };
}

function buildClientUploadKey(document) {
  const filename = String(document && document.filename ? document.filename : "").trim();
  if (filename && Number.isFinite(document && document.client_modified_ms)) {
    return `upload:name:${filename}::mtime:${Number(document.client_modified_ms)}`;
  }

  const rawPath = String(document && document.client_path ? document.client_path : filename).trim().replace(/\\/g, "/");
  const normalizedPath = rawPath
    .split("/")
    .map((segment) => segment.trim())
    .filter(Boolean)
    .join("/");
  if (!normalizedPath) {
    return null;
  }
  return `upload:path:${normalizedPath}`;
}

function applyUploadSimilarityResolutions(documents, resolutionByUploadKey = {}, defaultPolicy = "warn") {
  return documents.map((document) => {
    const uploadKey = buildClientUploadKey(document);
    const resolution = uploadKey ? resolutionByUploadKey[uploadKey] : null;
    return {
      ...document,
      similarity_policy: resolution && resolution.policy ? resolution.policy : defaultPolicy,
      similarity_target_document_id:
        resolution && resolution.targetDocumentId ? resolution.targetDocumentId : null,
    };
  });
}

function buildSimilarUploadCandidateLabel(candidate, index) {
  const title = candidate && candidate.title ? candidate.title : candidate.document_id;
  const folder = candidate && candidate.folder ? ` in ${candidate.folder}` : "";
  const updatedAt = candidate && candidate.updated_at ? ` [updated ${candidate.updated_at}]` : "";
  return `${index + 1}. ${title}${folder}${updatedAt} (${candidate.document_id})`;
}

function resolveSingleSimilarUploadConflict(conflict) {
  const uploadName = conflict && conflict.upload_name ? conflict.upload_name : "uploaded file";
  const candidates = Array.isArray(conflict && conflict.candidates) ? conflict.candidates : [];

  if (candidates.length <= 1) {
    const candidate = candidates[0] || conflict;
    const title = candidate && candidate.title ? candidate.title : conflict.existing_title || conflict.existing_document_id;
    const folder = candidate && candidate.folder ? ` in ${candidate.folder}` : conflict.existing_folder ? ` in ${conflict.existing_folder}` : "";
    const shouldReplace = window.confirm(
      `A similar document named ${uploadName} already exists.\n\n` +
      `Replace ${title}${folder} and refresh embeddings?\n\n` +
      "Press OK to replace it.\n" +
      "Press Cancel to ignore this upload."
    );
    return {
      policy: shouldReplace ? "replace" : "ignore",
      targetDocumentId: shouldReplace
        ? (candidate && candidate.document_id ? candidate.document_id : conflict.existing_document_id || null)
        : null,
    };
  }

  const choices = candidates
    .slice(0, 8)
    .map((candidate, index) => buildSimilarUploadCandidateLabel(candidate, index))
    .join("\n");

  while (true) {
    const selection = window.prompt(
      `Several similar documents named ${uploadName} already exist.\n\n` +
      `${choices}\n\n` +
      "Enter the number to replace that document, or type 0 to ignore this upload.",
      "1"
    );
    if (selection === null) {
      return { policy: "ignore", targetDocumentId: null };
    }

    const normalized = String(selection).trim().toLowerCase();
    if (normalized === "0" || normalized === "ignore" || normalized === "skip") {
      return { policy: "ignore", targetDocumentId: null };
    }

    const selectedIndex = Number.parseInt(normalized, 10);
    if (Number.isFinite(selectedIndex) && selectedIndex >= 1 && selectedIndex <= candidates.length) {
      return {
        policy: "replace",
        targetDocumentId: candidates[selectedIndex - 1].document_id,
      };
    }

    window.alert("Enter one of the listed numbers, or 0 to ignore this upload.");
  }
}

function collectSimilarUploadResolutions(conflicts) {
  const resolutions = {};
  if (!Array.isArray(conflicts)) {
    return resolutions;
  }

  conflicts.forEach((conflict) => {
    if (!conflict || !conflict.upload_key) {
      return;
    }
    resolutions[conflict.upload_key] = resolveSingleSimilarUploadConflict(conflict);
  });
  return resolutions;
}

function cloneContextFilter(filter) {
  return {
    folderIds: [...filter.folderIds],
    documentIds: [...filter.documentIds],
  };
}

function normalizeConversationPreferences(preferences) {
  const contextFilter = preferences && preferences.contextFilter
    ? preferences.contextFilter
    : {};
  return {
    sourceMode: preferences && preferences.sourceMode === "broader" ? "broader" : "internal",
    contextFilter: {
      folderIds: normalizeItems(
        (Array.isArray(contextFilter.folderIds) ? contextFilter.folderIds : [])
          .map((item) => String(item || "").trim())
          .filter(Boolean)
      ),
      documentIds: normalizeItems(
        (Array.isArray(contextFilter.documentIds) ? contextFilter.documentIds : [])
          .map((item) => String(item || "").trim())
          .filter(Boolean)
      ),
    },
    updatedAt: Number(preferences && preferences.updatedAt) || 0,
  };
}

function readConversationPreferenceStore() {
  try {
    const payload = JSON.parse(window.localStorage.getItem(CHAT_PREFERENCES_STORAGE_KEY) || "null");
    if (!payload || typeof payload !== "object") {
      return { lastUsed: null, conversations: {} };
    }
    return {
      lastUsed: payload.lastUsed ? normalizeConversationPreferences(payload.lastUsed) : null,
      conversations:
        payload.conversations && typeof payload.conversations === "object"
          ? payload.conversations
          : {},
    };
  } catch {
    return { lastUsed: null, conversations: {} };
  }
}

function writeConversationPreferenceStore(store) {
  try {
    const conversations = Object.fromEntries(
      Object.entries(store.conversations || {})
        .map(([conversationId, preferences]) => [
          conversationId,
          normalizeConversationPreferences(preferences),
        ])
        .sort((left, right) => right[1].updatedAt - left[1].updatedAt)
        .slice(0, MAX_STORED_CHAT_PREFERENCES)
    );
    window.localStorage.setItem(
      CHAT_PREFERENCES_STORAGE_KEY,
      JSON.stringify({
        lastUsed: store.lastUsed
          ? normalizeConversationPreferences(store.lastUsed)
          : null,
        conversations,
      })
    );
  } catch {
    // Browser storage can be unavailable. Saved chats still persist settings on the server.
  }
}

function getCurrentConversationPreferences() {
  return normalizeConversationPreferences({
    sourceMode: state.sourceMode,
    contextFilter: state.library.appliedContext,
  });
}

function rememberConversationPreferences(conversationId, preferences = getCurrentConversationPreferences()) {
  if (!conversationId) {
    return normalizeConversationPreferences(preferences);
  }
  const store = readConversationPreferenceStore();
  const normalized = {
    ...normalizeConversationPreferences(preferences),
    updatedAt: Date.now(),
  };
  store.lastUsed = normalized;
  store.conversations[conversationId] = normalized;
  writeConversationPreferenceStore(store);
  return normalized;
}

function getLastUsedConversationPreferences() {
  const store = readConversationPreferenceStore();
  return normalizeConversationPreferences(store.lastUsed || {});
}

function resolveSavedConversationPreferences(payload) {
  const serverPreferences = normalizeConversationPreferences({
    sourceMode: payload.source_mode,
    contextFilter: {
      folderIds: payload.context_filter?.folder_ids || [],
      documentIds: payload.context_filter?.document_ids || [],
    },
    updatedAt: Date.parse(payload.updated_at || "") || 0,
  });
  const stored = readConversationPreferenceStore().conversations[payload.conversation_id];
  const localPreferences = stored ? normalizeConversationPreferences(stored) : null;
  return localPreferences && localPreferences.updatedAt > serverPreferences.updatedAt
    ? localPreferences
    : serverPreferences;
}

function forgetConversationPreferences(conversationId) {
  const store = readConversationPreferenceStore();
  delete store.conversations[conversationId];
  writeConversationPreferenceStore(store);
}

async function syncConversationPreferences(conversationId, preferences) {
  try {
    const normalized = normalizeConversationPreferences(preferences);
    const response = await fetch("/api/conversations/settings", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        conversation_id: conversationId,
        source_mode: normalized.sourceMode,
        context_filter: {
          folder_ids: normalized.contextFilter.folderIds,
          document_ids: normalized.contextFilter.documentIds,
        },
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Chat settings update failed";
      throw new Error(detail);
    }
    if (payload && payload.conversation) {
      upsertSavedConversationSummary(buildConversationSummary(payload.conversation));
      renderSavedConversationList();
    }
  } catch (error) {
    if (conversationId === state.conversationId) {
      setConversationMemoryStatus(`Chat settings sync failed: ${error.message}`, "error");
    }
  }
}

function persistActiveConversationPreferences() {
  const conversationId = state.conversationId;
  const preferences = rememberConversationPreferences(conversationId);
  const existingTimer = conversationPreferenceSyncTimers.get(conversationId);
  if (existingTimer) {
    window.clearTimeout(existingTimer);
  }
  const timer = window.setTimeout(() => {
    conversationPreferenceSyncTimers.delete(conversationId);
    void syncConversationPreferences(conversationId, preferences);
  }, 200);
  conversationPreferenceSyncTimers.set(conversationId, timer);
}

function contextFiltersEqual(left, right) {
  return (
    normalizeItems(left.folderIds).join("|") === normalizeItems(right.folderIds).join("|") &&
    normalizeItems(left.documentIds).join("|") === normalizeItems(right.documentIds).join("|")
  );
}

function getDocumentSummary(documentId) {
  return state.library.documents.find((item) => item.document_id === documentId) || null;
}

function getFolderSummary(folderId) {
  return state.library.folders.find((item) => item.folder_id === folderId) || null;
}

function getPreviewDocument() {
  return state.library.previewCache[state.library.previewDocumentId] || null;
}

function hasGeneratedUploadId(documentId) {
  return typeof documentId === "string" && documentId.startsWith("UPL-");
}

function getDocumentDisplayLabel(documentLike) {
  if (!documentLike) {
    return "";
  }

  if (hasGeneratedUploadId(documentLike.document_id)) {
    return documentLike.title || documentLike.document_id;
  }

  return `${documentLike.document_id} - ${documentLike.title}`;
}

function getDocumentChipLabel(documentLike) {
  if (!documentLike) {
    return "";
  }

  if (hasGeneratedUploadId(documentLike.document_id)) {
    return `Doc: ${documentLike.title || documentLike.document_id}`;
  }

  return `Doc: ${documentLike.document_id}`;
}

function getFolderSegments(folderId) {
  const normalized = String(folderId || "").trim();
  if (!normalized) {
    return ["Unfiled"];
  }

  return normalized
    .split(/[\\/]+/)
    .map((segment) => segment.trim())
    .filter(Boolean);
}

function normalizeFolderPath(folderId) {
  return getFolderSegments(folderId).join("/");
}

function formatFolderPath(folderId) {
  return getFolderSegments(folderId).join(" / ");
}

function normalizeLocalSourcePath(sourcePath) {
  let normalized = String(sourcePath || "").trim();
  if (!normalized) {
    return "";
  }

  if (/^file:\/\//i.test(normalized)) {
    try {
      normalized = decodeURIComponent(new URL(normalized).pathname);
    } catch (_error) {
      normalized = normalized.replace(/^file:\/\/+/, "");
    }
  }

  normalized = normalized.replace(/^\/(?:[A-Za-z]:\/)/, (match) => match.slice(1));
  return normalized.replace(/[\\/]+/g, "/").replace(/\/$/, "").toLowerCase();
}

function getResolvedWatchedLibraryFolder(watchFolder) {
  const configuredFolder = normalizeFolderPath(watchFolder?.library_folder);
  if (state.library.folders.some((folder) => normalizeFolderPath(folder.folder_id) === configuredFolder)) {
    return configuredFolder;
  }

  const sourceRoot = normalizeLocalSourcePath(watchFolder?.source_path);
  if (!sourceRoot) {
    return configuredFolder;
  }

  const matchingFolders = state.library.documents
    .filter((documentSummary) => {
      const documentSource = normalizeLocalSourcePath(documentSummary.source_url);
      return documentSource === sourceRoot || documentSource.startsWith(`${sourceRoot}/`);
    })
    .map((documentSummary) => getFolderSegments(documentSummary.folder));

  if (matchingFolders.length === 0) {
    return configuredFolder;
  }

  const commonSegments = [...matchingFolders[0]];
  matchingFolders.slice(1).forEach((segments) => {
    while (
      commonSegments.length > 0 &&
      commonSegments.some((segment, index) => segment !== segments[index])
    ) {
      commonSegments.pop();
    }
  });
  return commonSegments.join("/") || configuredFolder;
}

function getWatchedFolderForLibraryFolder(folderId) {
  if (!folderId) {
    return null;
  }
  const normalizedFolderId = normalizeFolderPath(folderId);
  return state.library.watchFolders.find(
    (watchFolder) => getResolvedWatchedLibraryFolder(watchFolder) === normalizedFolderId
  ) || null;
}

function getWatchedFoldersWithinLibraryFolder(folderId) {
  const normalizedFolderId = normalizeFolderPath(folderId);
  if (!normalizedFolderId) {
    return [];
  }
  return state.library.watchFolders.filter((watchFolder) => {
    const watchedLibraryFolder = getResolvedWatchedLibraryFolder(watchFolder);
    return (
      watchedLibraryFolder === normalizedFolderId ||
      watchedLibraryFolder.startsWith(`${normalizedFolderId}/`)
    );
  });
}

function getFolderDisplayName(folderId, fallbackName = "") {
  const watchedFolder = getWatchedFolderForLibraryFolder(folderId);
  if (!watchedFolder) {
    return fallbackName || getFolderNameSegment(folderId);
  }

  const alias = String(watchedFolder.alias || "").trim();
  if (alias) {
    return alias;
  }

  const displayName = String(watchedFolder.display_name || "").trim();
  const normalizedDisplayName = normalizeFolderPath(displayName);
  const normalizedLibraryFolder = normalizeFolderPath(watchedFolder.library_folder);
  if (displayName && normalizedDisplayName !== normalizedLibraryFolder) {
    return displayName;
  }
  return fallbackName || getFolderNameSegment(folderId);
}

function formatFolderDisplayPath(folderId) {
  const segments = getFolderSegments(folderId);
  let currentPath = "";
  return segments
    .map((segment) => {
      currentPath = currentPath ? `${currentPath}/${segment}` : segment;
      return getFolderDisplayName(currentPath, segment);
    })
    .join(" / ");
}

function folderPathContainsFolder(parentFolderId, candidateFolderId) {
  const normalizedParent = normalizeFolderPath(parentFolderId);
  const normalizedCandidate = normalizeFolderPath(candidateFolderId);
  return (
    normalizedCandidate === normalizedParent ||
    normalizedCandidate.startsWith(`${normalizedParent}/`)
  );
}

function documentMatchesContext(documentSummary, context) {
  if (!context.folderIds.length && !context.documentIds.length) {
    return true;
  }

  return (
    context.documentIds.includes(documentSummary.document_id) ||
    context.folderIds.some((folderId) => folderPathContainsFolder(folderId, documentSummary.folder))
  );
}

function normalizeFolderInputValue(value, fallback = "uploaded") {
  const normalized = String(value || "").trim();
  if (!normalized) {
    return normalizeFolderPath(fallback);
  }
  return normalizeFolderPath(normalized);
}

function getFolderNameSegment(folderId) {
  const segments = getFolderSegments(folderId);
  return segments[segments.length - 1] || "";
}

function replaceFolderPathPrefix(pathId, oldPrefix, newPrefix) {
  const normalizedPathId = normalizeFolderPath(pathId);
  const normalizedOldPrefix = normalizeFolderPath(oldPrefix);
  const normalizedNewPrefix = normalizeFolderPath(newPrefix);

  if (normalizedPathId === normalizedOldPrefix) {
    return normalizedNewPrefix;
  }
  if (normalizedPathId.startsWith(`${normalizedOldPrefix}/`)) {
    return `${normalizedNewPrefix}${normalizedPathId.slice(normalizedOldPrefix.length)}`;
  }
  return normalizedPathId;
}

function remapFolderIdsAfterRename(folderIds, oldFolderId, newFolderId) {
  return normalizeItems(
    folderIds.map((folderId) => replaceFolderPathPrefix(folderId, oldFolderId, newFolderId))
  );
}

function syncFolderRenameAcrossState(oldFolderId, newFolderId) {
  state.library.appliedContext.folderIds = remapFolderIdsAfterRename(
    state.library.appliedContext.folderIds,
    oldFolderId,
    newFolderId
  );
  state.library.draftContext.folderIds = remapFolderIdsAfterRename(
    state.library.draftContext.folderIds,
    oldFolderId,
    newFolderId
  );

  if (state.library.activeFolderId) {
    state.library.activeFolderId = replaceFolderPathPrefix(
      state.library.activeFolderId,
      oldFolderId,
      newFolderId
    );
  }
}

function expandFolderAncestors(folderId) {
  const segments = getFolderSegments(folderId);
  if (!segments.length) {
    return;
  }

  let currentPath = "";
  const expandedIds = segments.map((segment) => {
    currentPath = currentPath ? `${currentPath}/${segment}` : segment;
    return currentPath;
  });
  state.library.collapsedFolderIds = normalizeItems(
    state.library.collapsedFolderIds.filter((folderIdItem) => !expandedIds.includes(folderIdItem))
  );
}

function getFolderDocumentIds(folderId) {
  return state.library.documents
    .filter((documentSummary) => folderPathContainsFolder(folderId, documentSummary.folder))
    .map((documentSummary) => documentSummary.document_id);
}

function getFolderDocuments(folderId) {
  if (!folderId) {
    return [];
  }

  const normalizedQuery = state.library.searchQuery.trim().toLowerCase();
  return state.library.documents
    .filter((documentSummary) => normalizeFolderPath(documentSummary.folder) === folderId)
    .filter((documentSummary) => {
      if (!normalizedQuery) {
        return true;
      }
      const haystack = [
        documentSummary.document_id,
        documentSummary.title,
        documentSummary.category,
        documentSummary.folder,
        documentSummary.summary,
        ...(documentSummary.tags || []),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(normalizedQuery);
    })
    .sort((left, right) => getDocumentDisplayLabel(left).localeCompare(getDocumentDisplayLabel(right)));
}

function setExplorerDragData(event, payload) {
  state.library.dragPayload = payload;
  if (!event.dataTransfer) {
    return;
  }
  event.dataTransfer.effectAllowed = "move";
  try {
    event.dataTransfer.setData("text/plain", JSON.stringify(payload));
  } catch {
    // Some browser/runtime combinations do not expose the payload during dragover.
  }
}

function getExplorerDragData(event) {
  if (state.library.dragPayload) {
    return state.library.dragPayload;
  }
  if (!event.dataTransfer) {
    return null;
  }

  try {
    const raw = event.dataTransfer.getData("text/plain");
    if (!raw) {
      return null;
    }
    const payload = JSON.parse(raw);
    if (!payload || typeof payload !== "object") {
      return null;
    }
    return payload;
  } catch {
    return null;
  }
}

function isValidFolderDropTarget(payload, targetFolderId) {
  if (!payload || payload.type !== "folder") {
    return false;
  }
  if (!payload.folderId || payload.folderId === targetFolderId) {
    return false;
  }
  if (targetFolderId && folderPathContainsFolder(payload.folderId, targetFolderId)) {
    return false;
  }
  return true;
}

function isValidDocumentDropTarget(payload, targetFolderId) {
  if (!payload || payload.type !== "document") {
    return false;
  }
  if (!targetFolderId) {
    return false;
  }
  return normalizeFolderPath(payload.currentFolderId) !== normalizeFolderPath(targetFolderId);
}

function clearDropTargetStyles() {
  document.querySelectorAll(".is-drop-target").forEach((element) => {
    element.classList.remove("is-drop-target");
  });
}

function clearExplorerDragData() {
  state.library.dragPayload = null;
}

function bindExplorerDragSource(element, payload, dragClassTarget = element) {
  if (!element || !canMutateLibrary()) {
    return;
  }

  element.draggable = true;
  element.addEventListener("dragstart", (event) => {
    dragClassTarget.classList.add("is-dragging");
    setExplorerDragData(event, payload);
  });
  element.addEventListener("dragend", () => {
    dragClassTarget.classList.remove("is-dragging");
    clearExplorerDragData();
    clearDropTargetStyles();
  });
}

function closeExplorerContextMenu() {
  state.library.contextMenu = {
    open: false,
    targetType: null,
    targetId: null,
    label: "",
    x: 0,
    y: 0,
  };
  explorerContextMenu.classList.add("is-hidden");
  explorerContextMenu.setAttribute("aria-hidden", "true");
  explorerContextMenu.style.left = "";
  explorerContextMenu.style.top = "";
}

function openExplorerContextMenu({ targetType, targetId, label, x, y }) {
  const canRename = targetType === "folder" || targetType === "document";
  const canCreateFolder = targetType === "folder" || targetType === "folder-space";
  const canDelete = targetType === "folder" || targetType === "document";

  state.library.contextMenu = {
    open: true,
    targetType,
    targetId,
    label: label || "",
    x,
    y,
  };

  explorerContextNewFolderButton.classList.toggle("is-hidden", !canCreateFolder);
  explorerContextNewFolderButton.disabled = !canCreateFolder;
  explorerContextNewFolderButton.textContent = targetType === "folder" ? "New subfolder" : "New folder";

  explorerContextRenameButton.classList.toggle("is-hidden", !canRename);
  explorerContextRenameButton.disabled = !canRename;
  explorerContextRenameButton.textContent = targetType === "folder"
    ? (getWatchedFolderForLibraryFolder(targetId) ? "Rename display alias" : "Rename folder")
    : "Rename file";

  explorerContextDeleteButton.classList.toggle("is-hidden", !canDelete);
  explorerContextDeleteButton.disabled = !canDelete;
  explorerContextDeleteButton.textContent = targetType === "folder" ? "Delete folder" : "Delete file";
  explorerContextMenu.classList.remove("is-hidden");
  explorerContextMenu.setAttribute("aria-hidden", "false");

  const menuWidth = Math.max(180, explorerContextMenu.offsetWidth);
  const menuHeight = explorerContextMenu.offsetHeight;
  const left = Math.max(12, Math.min(x, window.innerWidth - menuWidth - 12));
  const top = Math.max(12, Math.min(y, window.innerHeight - menuHeight - 12));
  explorerContextMenu.style.left = `${left}px`;
  explorerContextMenu.style.top = `${top}px`;
}

function getFolderTreeNodes() {
  const root = {
    pathId: null,
    label: "All documents",
    depth: 0,
    children: [],
    folder: null,
    exactDocumentCount: 0,
    totalDocumentCount: 0,
  };
  const nodesByPath = new Map();
  nodesByPath.set("", root);

  state.library.folders.forEach((folder) => {
    const segments = getFolderSegments(folder.folder_id);
    let parent = root;
    let currentPath = "";

    segments.forEach((segment, index) => {
      currentPath = currentPath ? `${currentPath}/${segment}` : segment;
      let node = nodesByPath.get(currentPath);
      if (!node) {
        node = {
          pathId: currentPath,
          label: segment,
          depth: index + 1,
          children: [],
          folder: null,
          exactDocumentCount: 0,
          totalDocumentCount: 0,
        };
        nodesByPath.set(currentPath, node);
        parent.children.push(node);
      }
      parent = node;
    });

    parent.folder = folder;
    parent.exactDocumentCount = folder.document_count;
  });

  const finalizeNode = (node) => {
    node.children.sort((left, right) => left.label.localeCompare(right.label));
    const childDocumentCount = node.children.reduce((sum, child) => sum + finalizeNode(child), 0);
    node.totalDocumentCount = node.exactDocumentCount + childDocumentCount;
    return node.totalDocumentCount;
  };

  finalizeNode(root);
  return root;
}

function isFolderCollapsed(pathId) {
  return state.library.collapsedFolderIds.includes(pathId);
}

function toggleFolderCollapsed(pathId) {
  const nextIds = new Set(state.library.collapsedFolderIds);
  if (nextIds.has(pathId)) {
    nextIds.delete(pathId);
  } else {
    nextIds.add(pathId);
  }
  state.library.collapsedFolderIds = normalizeItems(Array.from(nextIds));
  renderFolderTree();
}

function getAllLibraryFolderPathIds() {
  return normalizeItems(
    state.library.folders.map((item) => normalizeFolderPath(item.folder_id)).filter(Boolean)
  );
}

function setAllFoldersCollapsed(isCollapsed) {
  if (!isCollapsed) {
    state.library.collapsedFolderIds = [];
  } else {
    state.library.collapsedFolderIds = getAllLibraryFolderPathIds();
  }
  renderFolderTree();
}

function setActiveFolder(pathId) {
  state.library.activeFolderId = pathId;
  state.library.previewDocumentId = null;
  state.library.editorDismissed = false;
  renderBrowserStats();
  renderFolderTree();
  renderLibraryBreadcrumbs();
  renderDocumentFileList();
  renderDocumentEditor();
  renderPreview();
}

function matchesActiveFolder(documentSummary) {
  if (!state.library.activeFolderId) {
    return true;
  }

  const documentPathId = normalizeFolderPath(documentSummary.folder);
  return (
    documentPathId === state.library.activeFolderId ||
    documentPathId.startsWith(`${state.library.activeFolderId}/`)
  );
}

function getVisibleDocuments() {
  const normalizedQuery = state.library.searchQuery.trim().toLowerCase();
  return [...state.library.documents]
    .filter((documentSummary) => matchesActiveFolder(documentSummary))
    .filter((documentSummary) => {
      if (!normalizedQuery) {
        return true;
      }

      const haystack = [
        documentSummary.document_id,
        documentSummary.title,
        documentSummary.category,
        documentSummary.folder,
        documentSummary.summary,
        ...(documentSummary.tags || []),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(normalizedQuery);
    })
    .sort((left, right) => {
      const folderCompare = formatFolderPath(left.folder).localeCompare(formatFolderPath(right.folder));
      if (folderCompare !== 0) {
        return folderCompare;
      }
      return getDocumentDisplayLabel(left).localeCompare(getDocumentDisplayLabel(right));
    });
}

function getDocumentKindLabel(documentLike) {
  const candidates = [documentLike.title, documentLike.document_id];
  for (const candidate of candidates) {
    const match = String(candidate || "").match(/\.([A-Za-z0-9]{1,6})$/);
    if (match) {
      return match[1].toUpperCase();
    }
  }
  return String(documentLike.category || "DOC").slice(0, 6).toUpperCase();
}

function canMutateLibrary() {
  return state.library.backend === "json" || state.library.backend === "semantic";
}

async function parseJsonResponse(response) {
  const responseText = await response.text();
  if (!responseText) {
    return null;
  }

  try {
    return JSON.parse(responseText);
  } catch {
    return null;
  }
}

function normalizeConversationMessage(message) {
  const role = message && typeof message.role === "string" ? message.role : "assistant";
  return {
    role,
    label:
      typeof message.label === "string" && message.label
        ? message.label
        : role === "user"
          ? "You"
          : role === "system"
            ? "System"
            : "Assistant",
    body: typeof message.body === "string" ? message.body : "",
    citations: Array.isArray(message && message.citations)
      ? message.citations.map((item) => ({ ...item }))
      : [],
    toolTrace: Array.isArray(message && message.toolTrace)
      ? message.toolTrace.map((item) => ({ ...item }))
      : Array.isArray(message && message.tool_trace)
        ? message.tool_trace.map((item) => ({ ...item }))
        : [],
    generatedDocument:
      message && message.generatedDocument
        ? { ...message.generatedDocument }
        : message && message.generated_document
          ? { ...message.generated_document }
          : null,
  };
}

function hasConversationMessages() {
  return state.messages.length > 0;
}

function getSavedConversationSummary(conversationId) {
  return state.memory.conversations.find((item) => item.conversation_id === conversationId) || null;
}

function sortSavedConversations(conversations) {
  return [...conversations].sort((left, right) =>
    String(right.updated_at || "").localeCompare(String(left.updated_at || ""))
  );
}

function buildConversationSummary(conversation) {
  return {
    conversation_id: conversation.conversation_id,
    title: conversation.title || null,
    title_is_custom: Boolean(conversation.title_is_custom),
    summary: typeof conversation.summary === "string" && conversation.summary.trim()
      ? conversation.summary.trim()
      : "Saved conversation",
    created_at: conversation.created_at,
    updated_at: conversation.updated_at,
    message_count: conversation.message_count,
    source_mode: conversation.source_mode,
  };
}

function getSavedConversationDisplayLabel(conversation) {
  if (conversation && conversation.title_is_custom && String(conversation.title || "").trim()) {
    return String(conversation.title).trim();
  }
  if (conversation && String(conversation.summary || "").trim()) {
    return String(conversation.summary).trim();
  }
  if (conversation && String(conversation.title || "").trim()) {
    return String(conversation.title).trim();
  }
  return "Saved conversation";
}

function getVisibleSavedConversations() {
  const query = state.memory.searchQuery.trim().toLowerCase();
  if (!query) {
    return state.memory.conversations;
  }

  return state.memory.conversations.filter((conversation) => {
    const searchable = [
      getSavedConversationDisplayLabel(conversation),
      conversation.summary,
      conversation.title,
      conversation.source_mode,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return searchable.includes(query);
  });
}

function upsertSavedConversationSummary(summary) {
  const nextItems = state.memory.conversations.filter(
    (item) => item.conversation_id !== summary.conversation_id
  );
  nextItems.push(summary);
  state.memory.conversations = sortSavedConversations(nextItems);
}

function formatConversationTimestamp(timestamp) {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return "Unknown update time";
  }
  return date.toLocaleString([], {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function reconcileContextFilterWithLibrary(filter) {
  const validFolderIds = new Set(state.library.folders.map((item) => item.folder_id));
  const validDocumentIds = new Set(state.library.documents.map((item) => item.document_id));
  return {
    folderIds: normalizeItems(filter.folderIds.filter((item) => validFolderIds.has(item))),
    documentIds: normalizeItems(filter.documentIds.filter((item) => validDocumentIds.has(item))),
  };
}

function reconcileLibraryState() {
  const validDocumentIds = new Set(state.library.documents.map((item) => item.document_id));
  const validFolderPathIds = new Set(state.library.folders.map((item) => normalizeFolderPath(item.folder_id)));

  state.library.appliedContext = reconcileContextFilterWithLibrary(state.library.appliedContext);
  state.library.draftContext = reconcileContextFilterWithLibrary(state.library.draftContext);
  state.library.deleteSelectionIds = normalizeItems(
    state.library.deleteSelectionIds.filter((item) => validDocumentIds.has(item))
  );
  state.library.collapsedFolderIds = normalizeItems(
    state.library.collapsedFolderIds.filter((item) => validFolderPathIds.has(item))
  );

  if (state.library.activeFolderId && !validFolderPathIds.has(state.library.activeFolderId)) {
    state.library.activeFolderId = null;
  }

  if (state.library.previewDocumentId && !validDocumentIds.has(state.library.previewDocumentId)) {
    state.library.previewDocumentId = null;
    state.library.editorDismissed = false;
  }
  if (state.library.editorDocumentId && !validDocumentIds.has(state.library.editorDocumentId)) {
    state.library.editorDocumentId = null;
    state.library.editorDirty = false;
  }

  Object.keys(state.library.previewCache).forEach((documentId) => {
    if (!validDocumentIds.has(documentId)) {
      delete state.library.previewCache[documentId];
    }
  });
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderInlineMarkdown(text) {
  const placeholders = [];
  const stash = (html) => {
    const token = `@@MD${placeholders.length}@@`;
    placeholders.push(html);
    return token;
  };

  let rendered = escapeHtml(text);

  rendered = rendered.replace(/`([^`\n]+)`/g, (_, codeText) => {
    return stash(`<code>${codeText}</code>`);
  });

  rendered = rendered.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, label, url) => stash(`<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`)
  );

  rendered = rendered.replace(/\*\*([\s\S]+?)\*\*/g, "<strong>$1</strong>");
  rendered = rendered.replace(/__([\s\S]+?)__/g, "<strong>$1</strong>");
  rendered = rendered.replace(/~~([\s\S]+?)~~/g, "<del>$1</del>");

  return rendered.replace(/@@MD(\d+)@@/g, (_, index) => placeholders[Number(index)] || "");
}

function renderMarkdown(markdown) {
  const normalized = String(markdown || "").replace(/\r\n?/g, "\n").trim();
  if (!normalized) {
    return "";
  }

  const lines = normalized.split("\n");
  const blocks = [];
  let paragraph = [];
  let unorderedList = [];
  let orderedList = [];
  let blockquote = [];
  let codeLines = [];
  let inCodeBlock = false;

  const flushParagraph = () => {
    if (paragraph.length === 0) {
      return;
    }
    blocks.push(`<p>${paragraph.map((line) => renderInlineMarkdown(line)).join("<br>")}</p>`);
    paragraph = [];
  };

  const flushUnorderedList = () => {
    if (unorderedList.length === 0) {
      return;
    }
    blocks.push(
      `<ul>${unorderedList.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`
    );
    unorderedList = [];
  };

  const flushOrderedList = () => {
    if (orderedList.length === 0) {
      return;
    }
    blocks.push(
      `<ol>${orderedList.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ol>`
    );
    orderedList = [];
  };

  const flushBlockquote = () => {
    if (blockquote.length === 0) {
      return;
    }
    blocks.push(`<blockquote>${blockquote.map((line) => renderInlineMarkdown(line)).join("<br>")}</blockquote>`);
    blockquote = [];
  };

  const flushTextBlocks = () => {
    flushParagraph();
    flushUnorderedList();
    flushOrderedList();
    flushBlockquote();
  };

  const flushCodeBlock = () => {
    if (codeLines.length === 0) {
      return;
    }
    blocks.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
    codeLines = [];
  };

  lines.forEach((line) => {
    if (line.trim().startsWith("```")) {
      if (inCodeBlock) {
        flushCodeBlock();
        inCodeBlock = false;
      } else {
        flushTextBlocks();
        inCodeBlock = true;
      }
      return;
    }

    if (inCodeBlock) {
      codeLines.push(line);
      return;
    }

    if (!line.trim()) {
      flushTextBlocks();
      return;
    }

    const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      flushTextBlocks();
      const level = headingMatch[1].length;
      blocks.push(`<h${level}>${renderInlineMarkdown(headingMatch[2].trim())}</h${level}>`);
      return;
    }

    const unorderedMatch = line.match(/^\s*[-*]\s+(.+)$/);
    if (unorderedMatch) {
      flushParagraph();
      flushOrderedList();
      flushBlockquote();
      unorderedList.push(unorderedMatch[1]);
      return;
    }

    const orderedMatch = line.match(/^\s*\d+\.\s+(.+)$/);
    if (orderedMatch) {
      flushParagraph();
      flushUnorderedList();
      flushBlockquote();
      orderedList.push(orderedMatch[1]);
      return;
    }

    const quoteMatch = line.match(/^\s*>\s?(.*)$/);
    if (quoteMatch) {
      flushParagraph();
      flushUnorderedList();
      flushOrderedList();
      blockquote.push(quoteMatch[1]);
      return;
    }

    paragraph.push(line);
  });

  if (inCodeBlock) {
    flushCodeBlock();
  }
  flushTextBlocks();
  return blocks.join("");
}

function renderMessage(message, options = {}) {
  const normalized = normalizeConversationMessage(message);
  if (options.persist !== false) {
    state.messages.push(normalized);
  }

  const node = messageTemplate.content.firstElementChild.cloneNode(true);
  node.classList.add(normalized.role);
  node.querySelector(".message-meta").textContent = normalized.label;
  node.querySelector(".message-body").innerHTML = renderMarkdown(normalized.body);

  const footer = node.querySelector(".message-footer");

  if (normalized.generatedDocument) {
    const generatedDocumentSection = document.createElement("div");
    generatedDocumentSection.className = "generated-document-section";

    const generatedDocumentLabel = document.createElement("div");
    generatedDocumentLabel.className = "generated-document-title";
    generatedDocumentLabel.textContent = "Generated file:";
    generatedDocumentSection.appendChild(generatedDocumentLabel);

    const downloadButton = document.createElement("button");
    downloadButton.type = "button";
    downloadButton.className = "generated-document-button";
    downloadButton.textContent = normalized.generatedDocument.title
      ? `${normalized.generatedDocument.title} (${normalized.generatedDocument.filename})`
      : normalized.generatedDocument.filename;
    downloadButton.addEventListener("click", () => {
      downloadBase64File(
        normalized.generatedDocument.filename,
        normalized.generatedDocument.mime_type,
        normalized.generatedDocument.content_base64
      );
    });
    generatedDocumentSection.appendChild(downloadButton);
    footer.appendChild(generatedDocumentSection);
  }

  if (normalized.citations.length > 0) {
    const citationLabel = document.createElement("div");
    citationLabel.className = "citation-section-title";
    citationLabel.textContent = "Citations:";
    footer.appendChild(citationLabel);

    const citationList = document.createElement("div");
    citationList.className = "citation-list";

    normalized.citations.forEach((citation) => {
      const isWebCitation = citation.category === "web" && /^https?:\/\//i.test(citation.source_url || "");
      if (isWebCitation) {
        const target = document.createElement("a");
        target.className = "citation citation-link";
        target.href = citation.source_url;
        target.target = "_blank";
        target.rel = "noopener noreferrer";
        target.textContent = citation.title || citation.source_url;
        target.title = `Open web source: ${citation.title || citation.source_url}`;
        citationList.appendChild(target);
        return;
      }

      const target = document.createElement("button");
      target.type = "button";
      target.className = "citation citation-button";
      target.dataset.documentId = citation.document_id;
      target.textContent = getDocumentDisplayLabel(citation);
      target.title = `Open ${citation.document_id}`;
      target.addEventListener("click", async () => {
        await openCitationDocument(citation);
      });
      citationList.appendChild(target);
    });

    footer.appendChild(citationList);
  }

  messageList.appendChild(node);
  if (options.scrollToLatest !== false) {
    messageList.scrollTop = messageList.scrollHeight;
  }
  renderConversationSaveButton();
}

function hideResponsePreparationIndicator() {
  if (!state.responseIndicatorNode) {
    return;
  }
  state.responseIndicatorNode.remove();
  state.responseIndicatorNode = null;
}

function showResponsePreparationIndicator() {
  hideResponsePreparationIndicator();

  const node = messageTemplate.content.firstElementChild.cloneNode(true);
  node.classList.add("assistant", "preparing");
  node.querySelector(".message-meta").textContent = "Assistant";
  node.querySelector(".message-body").innerHTML = `
    <div class="response-preparing" role="status" aria-live="polite" aria-label="Assistant is preparing a response">
      <span class="response-preparing-text">Preparing response</span>
      <span class="response-preparing-dots" aria-hidden="true">
        <span></span>
        <span></span>
        <span></span>
      </span>
    </div>
  `;
  node.querySelector(".message-footer").remove();

  state.responseIndicatorNode = node;
  messageList.appendChild(node);
  messageList.scrollTop = messageList.scrollHeight;
}

async function openCitationDocument(citation) {
  if (!citation || !citation.document_id) {
    return;
  }

  if (
    !state.library.loaded ||
    state.library.loadError ||
    !getDocumentSummary(citation.document_id)
  ) {
    await loadDocumentLibrary();
  }

  openDocumentBrowser();
  await openDocumentPreview(citation.document_id, { revealInExplorer: true });
}

function setComposerState(isSending) {
  state.sending = isSending;
  sendButton.disabled = isSending;
  messageInput.disabled = isSending;
  sendButton.textContent = isSending ? "Sending..." : "Send";
  renderConversationSaveButton();
}

function setUploadState(isUploading) {
  state.library.uploadInFlight = isUploading;
  [
    uploadFileInput,
    uploadDirectoryInput,
    uploadCategoryInput,
    uploadFolderInput,
    uploadTitleInput,
    uploadTagsInput,
    selectUploadFilesButton,
    selectUploadDirectoryButton,
    uploadButton,
  ].forEach((element) => {
    element.disabled = isUploading;
  });
  uploadButton.textContent = isUploading ? "Importing..." : "Import to library";
}

function setUploadStatus(message, tone = "neutral") {
  uploadStatus.textContent = message;
  uploadStatus.classList.remove("is-error", "is-success");
  if (tone === "error") {
    uploadStatus.classList.add("is-error");
  }
  if (tone === "success") {
    uploadStatus.classList.add("is-success");
  }
}

function setWatchFolderState(isInFlight) {
  state.library.watchFolderInFlight = isInFlight;
  [
    watchRootPathInput,
    browseWatchRootPathButton,
    watchAliasInput,
    watchSubfolderInput,
    watchIntervalInput,
    watchLibraryFolderInput,
    watchCategoryInput,
    watchTagsInput,
    watchRecursiveInput,
    folderPropertiesAliasInput,
    folderPropertiesIntervalInput,
    folderPropertiesCategoryInput,
    folderPropertiesTagsInput,
    folderPropertiesRecursiveInput,
    folderPropertiesEnabledInput,
    addWatchFolderButton,
    syncAllWatchFoldersButton,
    librarySyncAllButton,
    syncFolderActionButton,
    addSynchronizedPathButton,
    openSynchronizedPathsButton,
  ].forEach((element) => {
    if (element) {
      element.disabled = isInFlight;
    }
  });
  if (addWatchFolderButton) {
    addWatchFolderButton.textContent = isInFlight ? "Working..." : "Add watcher";
  }
  if (syncAllWatchFoldersButton) {
    syncAllWatchFoldersButton.textContent = isInFlight ? "Syncing..." : "Sync all now";
  }
  if (librarySyncAllButton) {
    librarySyncAllButton.textContent = isInFlight ? "Syncing..." : "Sync all";
  }
  if (syncFolderActionButton) {
    syncFolderActionButton.classList.toggle("is-working", isInFlight);
  }
  renderWatchFolderList();
  renderDocumentEditor();
}

function setWatchFolderStatus(message, tone = "neutral") {
  if (!watchFolderStatus) {
    return;
  }
  watchFolderStatus.textContent = message;
  watchFolderStatus.classList.remove("is-error", "is-success");
  if (tone === "error") {
    watchFolderStatus.classList.add("is-error");
  }
  if (tone === "success") {
    watchFolderStatus.classList.add("is-success");
  }
}

function formatWatchFolderTimestamp(timestamp) {
  if (!timestamp) {
    return "Never";
  }
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return "Unknown";
  }
  return date.toLocaleString([], {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function openSynchronizedPathsMenu() {
  if (!synchronizedPathsMenu) {
    return;
  }
  if (!state.library.watchFoldersLoaded && !state.library.watchFoldersLoadError) {
    void loadWatchedFolders();
  }
  renderWatchFolderList();
  synchronizedPathsMenu.classList.remove("is-hidden");
  synchronizedPathsMenu.setAttribute("aria-hidden", "false");
}

function closeSynchronizedPathsMenu() {
  if (!synchronizedPathsMenu) {
    return;
  }
  synchronizedPathsMenu.classList.add("is-hidden");
  synchronizedPathsMenu.setAttribute("aria-hidden", "true");
}

function renderWatchFolderList() {
  const watchFolderCount = state.library.watchFolders.length;
  if (synchronizedPathsCount) {
    synchronizedPathsCount.textContent = state.library.watchFoldersLoaded
      ? String(watchFolderCount)
      : "...";
  }
  if (synchronizedPathsSummary) {
    synchronizedPathsSummary.textContent = state.library.watchFoldersLoaded
      ? `${watchFolderCount} source folder${watchFolderCount === 1 ? "" : "s"} configured for synchronization.`
      : "Loading synchronized paths...";
  }
  if (librarySyncAllButton) {
    librarySyncAllButton.disabled =
      state.library.watchFolderInFlight ||
      !state.library.watchFoldersLoaded ||
      state.library.watchFolders.length === 0;
  }
  if (!watchFolderList) {
    return;
  }
  watchFolderList.innerHTML = "";

  if (state.library.watchFoldersLoadError) {
    const error = document.createElement("p");
    error.className = "watch-folder-empty";
    error.textContent = `Watched folders unavailable: ${state.library.watchFoldersLoadError}`;
    watchFolderList.appendChild(error);
    return;
  }

  if (!state.library.watchFoldersLoaded) {
    const loading = document.createElement("p");
    loading.className = "watch-folder-empty";
    loading.textContent = "Loading watched folders...";
    watchFolderList.appendChild(loading);
    return;
  }

  if (state.library.watchFolders.length === 0) {
    const empty = document.createElement("p");
    empty.className = "watch-folder-empty";
    empty.textContent = "No synchronized folder paths are configured.";
    watchFolderList.appendChild(empty);
    return;
  }

  state.library.watchFolders.forEach((watchFolder) => {
    const item = document.createElement("article");
    item.className = "watch-folder-item";
    item.classList.toggle(
      "is-error",
      watchFolder.last_status === "error" || Number(watchFolder.last_error_count || 0) > 0
    );

    const body = document.createElement("div");
    body.className = "watch-folder-body";

    const title = document.createElement("h4");
    title.textContent = watchFolder.alias || watchFolder.display_name || watchFolder.source_path;
    body.appendChild(title);

    const pathLine = document.createElement("p");
    pathLine.className = "watch-folder-path";
    pathLine.textContent = watchFolder.source_path;
    body.appendChild(pathLine);

    const meta = document.createElement("p");
    meta.className = "watch-folder-meta";
    meta.textContent = [
      `Library: ${watchFolder.library_folder}`,
      `Every ${watchFolder.interval_minutes} min`,
      `Last: ${formatWatchFolderTimestamp(watchFolder.last_sync_at)}`,
    ].join(" - ");
    body.appendChild(meta);

    if (watchFolder.last_message) {
      const message = document.createElement("p");
      message.className = "watch-folder-message";
      message.textContent = watchFolder.last_message;
      body.appendChild(message);
    }

    const stats = document.createElement("p");
    stats.className = "watch-folder-meta";
    stats.textContent =
      `${watchFolder.last_scanned_count || 0} scanned - ` +
      `${watchFolder.last_created_count || 0} new - ` +
      `${watchFolder.last_updated_count || 0} updated - ` +
      `${watchFolder.last_skipped_count || 0} skipped - ` +
      `${watchFolder.last_error_count || 0} errors`;
    body.appendChild(stats);

    const actions = document.createElement("div");
    actions.className = "watch-folder-actions";

    const aliasButton = document.createElement("button");
    aliasButton.type = "button";
    aliasButton.className = "secondary-button compact-button";
    aliasButton.disabled = state.library.watchFolderInFlight;
    aliasButton.textContent = "Alias";
    aliasButton.addEventListener("click", () => {
      closeSynchronizedPathsMenu();
      beginWatchedFolderAliasEdit(watchFolder);
    });
    actions.appendChild(aliasButton);

    const syncButton = document.createElement("button");
    syncButton.type = "button";
    syncButton.className = "secondary-button compact-button";
    syncButton.disabled = state.library.watchFolderInFlight;
    syncButton.textContent = "Sync";
    syncButton.addEventListener("click", async () => {
      await syncWatchedFolder(watchFolder.watch_id);
    });
    actions.appendChild(syncButton);

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "danger-button compact-danger-button";
    deleteButton.disabled = state.library.watchFolderInFlight;
    deleteButton.textContent = "Unsynchronize";
    deleteButton.addEventListener("click", async () => {
      await deleteWatchedFolder(watchFolder.watch_id);
    });
    actions.appendChild(deleteButton);

    item.appendChild(body);
    item.appendChild(actions);
    watchFolderList.appendChild(item);
  });
}

async function loadWatchedFolders() {
  try {
    const response = await fetch("/api/watch-folders");
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(getErrorMessageFromPayload(payload, "Watched folders request failed"));
    }
    state.library.watchFolders = payload.watched_folders || [];
    state.library.watchFoldersLoaded = true;
    state.library.watchFoldersLoadError = null;
  } catch (error) {
    state.library.watchFolders = [];
    state.library.watchFoldersLoaded = true;
    state.library.watchFoldersLoadError = error.message;
  }
  renderWatchFolderList();
  renderFolderTree();
  renderLibraryBreadcrumbs();
  renderDocumentEditor();
}

async function selectAndCreateWatchedFolder() {
  if (state.library.watchFolderInFlight) {
    return;
  }

  setWatchFolderState(true);
  setLibraryActionStatus("Opening the system folder picker...");
  try {
    const browseResponse = await fetch("/api/local-folders/browse", { method: "POST" });
    const browsePayload = await parseJsonResponse(browseResponse);
    if (!browseResponse.ok) {
      throw new Error(getErrorMessageFromPayload(browsePayload, "Folder picker failed"));
    }
    if (browsePayload.cancelled) {
      setLibraryActionStatus(browsePayload.message || "Folder selection cancelled.");
      return;
    }
    if (!browsePayload.path) {
      throw new Error("The folder picker did not return a path.");
    }

    const normalizedSourcePath = normalizeLocalSourcePath(browsePayload.path);
    let watchedFolder = state.library.watchFolders.find(
      (item) => normalizeLocalSourcePath(item.source_path) === normalizedSourcePath
    ) || null;

    if (!watchedFolder) {
      setLibraryActionStatus("Adding the selected folder and preparing its first sync...");
      const createResponse = await fetch("/api/watch-folders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          root_path: browsePayload.path,
          category: "watched",
          tags: [],
          recursive: true,
          enabled: true,
          interval_minutes: 30,
        }),
      });
      const createPayload = await parseJsonResponse(createResponse);
      if (!createResponse.ok) {
        throw new Error(getErrorMessageFromPayload(createPayload, "Add watched folder failed"));
      }
      watchedFolder = createPayload.watched_folder;
    }

    setLibraryActionStatus("Synchronizing the selected folder...");
    const syncResponse = await fetch(
      `/api/watch-folders/${encodeURIComponent(watchedFolder.watch_id)}/sync`,
      { method: "POST" }
    );
    const syncPayload = await parseJsonResponse(syncResponse);
    if (!syncResponse.ok) {
      throw new Error(getErrorMessageFromPayload(syncPayload, "Watched folder sync failed"));
    }

    await loadWatchedFolders();
    await refreshLibraryAfterWatchSync(syncPayload.results || []);
    const currentWatcher = state.library.watchFolders.find(
      (item) => item.watch_id === watchedFolder.watch_id
    ) || watchedFolder;
    const folderId = getResolvedWatchedLibraryFolder(currentWatcher);
    if (folderId) {
      state.library.activeFolderId = folderId;
      state.library.previewDocumentId = null;
      expandFolderAncestors(folderId);
      renderLibraryExplorer();
      renderPreview();
    }
    setLibraryActionStatus(
      syncPayload.message || "Folder synchronized. Select it to edit synchronization settings.",
      "success"
    );
  } catch (error) {
    setLibraryActionStatus(error.message, "error");
  } finally {
    setWatchFolderState(false);
  }
}

async function refreshLibraryAfterWatchSync(results) {
  const changed = Array.isArray(results) && results.some((result) =>
    result.semantic_index_rebuilt ||
    Number(result.created_count || 0) > 0 ||
    Number(result.updated_count || 0) > 0
  );
  if (!changed) {
    return;
  }
  state.library.previewCache = {};
  await loadDocumentLibrary();
  renderPreview();
}

async function addWatchedFolder(event) {
  event.preventDefault();
  if (state.library.watchFolderInFlight) {
    return;
  }

  const rootPath = String(watchRootPathInput.value || "").trim();
  if (!rootPath) {
    setWatchFolderStatus("Project root path is required.", "error");
    return;
  }

  setWatchFolderState(true);
  setWatchFolderStatus("Adding watched folder...");

  try {
    const response = await fetch("/api/watch-folders", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        root_path: rootPath,
        alias: String(watchAliasInput.value || "").trim() || null,
        include_subfolder: String(watchSubfolderInput.value || "").trim() || null,
        library_folder: String(watchLibraryFolderInput.value || "").trim() || null,
        category: String(watchCategoryInput.value || "").trim() || "watched",
        tags: normalizeTagItems(watchTagsInput.value || ""),
        recursive: Boolean(watchRecursiveInput.checked),
        enabled: true,
        interval_minutes: Number.parseInt(watchIntervalInput.value || "30", 10) || 30,
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(getErrorMessageFromPayload(payload, "Add watched folder failed"));
    }
    watchFolderForm.reset();
    watchAliasInput.value = "";
    watchCategoryInput.value = "watched";
    watchIntervalInput.value = "30";
    watchRecursiveInput.checked = true;
    await loadWatchedFolders();
    setWatchFolderStatus(payload.message || "Watched folder added.", "success");
  } catch (error) {
    setWatchFolderStatus(error.message, "error");
  } finally {
    setWatchFolderState(false);
  }
}

async function browseForWatchRootPath() {
  if (state.library.watchFolderInFlight) {
    return;
  }

  setWatchFolderState(true);
  setWatchFolderStatus("Opening folder picker...");

  try {
    const response = await fetch("/api/local-folders/browse", {
      method: "POST",
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(getErrorMessageFromPayload(payload, "Folder picker failed"));
    }
    if (payload.cancelled) {
      setWatchFolderStatus(payload.message || "Folder selection cancelled.");
      return;
    }
    if (!payload.path) {
      throw new Error("The folder picker did not return a path.");
    }
    watchRootPathInput.value = payload.path;
    setWatchFolderStatus("Project root path selected.", "success");
    watchSubfolderInput.focus();
  } catch (error) {
    setWatchFolderStatus(error.message, "error");
  } finally {
    setWatchFolderState(false);
  }
}

function focusInlineFolderRename() {
  const input = folderTreeList.querySelector(".folder-tree-rename-input");
  if (!input) {
    return;
  }
  input.focus();
  input.select();
}

function beginFolderInlineRename(folderId) {
  if (!folderId || state.library.inlineRenameInFlight) {
    return;
  }

  const watchedFolder = getWatchedFolderForLibraryFolder(folderId);
  if (!watchedFolder && !canMutateLibrary()) {
    setLibraryActionStatus("Folder rename is only available for local json and semantic libraries.", "error");
    return;
  }

  const fallbackName = getFolderNameSegment(folderId);
  const currentDisplayName = getFolderDisplayName(folderId, fallbackName);
  state.library.inlineRenameFolderId = normalizeFolderPath(folderId);
  state.library.inlineRenameDraft = currentDisplayName;
  state.library.inlineRenameOriginalValue = currentDisplayName;
  renderFolderTree();
  focusInlineFolderRename();
  setLibraryActionStatus(
    watchedFolder
      ? "Rename the watched folder alias in place. Press Enter to save or Escape to cancel."
      : "Rename the folder in place. Press Enter to save or Escape to cancel."
  );
}

function cancelFolderInlineRename() {
  if (state.library.inlineRenameInFlight) {
    return;
  }
  state.library.inlineRenameFolderId = null;
  state.library.inlineRenameDraft = "";
  state.library.inlineRenameOriginalValue = "";
  renderFolderTree();
}

async function commitFolderInlineRename(folderId, nextNameValue) {
  if (
    !folderId ||
    state.library.inlineRenameFolderId !== normalizeFolderPath(folderId) ||
    state.library.inlineRenameInFlight
  ) {
    return false;
  }

  const nextName = String(nextNameValue || "").trim();
  if (nextName === state.library.inlineRenameOriginalValue) {
    cancelFolderInlineRename();
    return true;
  }

  state.library.inlineRenameDraft = nextName;
  state.library.inlineRenameInFlight = true;
  const input = folderTreeList.querySelector(".folder-tree-rename-input");
  if (input) {
    input.disabled = true;
  }

  const saved = await renameFolderFromContext(folderId, nextName);
  state.library.inlineRenameInFlight = false;
  if (saved) {
    state.library.inlineRenameFolderId = null;
    state.library.inlineRenameDraft = "";
    state.library.inlineRenameOriginalValue = "";
    renderFolderTree();
    return true;
  }

  renderFolderTree();
  focusInlineFolderRename();
  return false;
}

function beginWatchedFolderAliasEdit(watchFolder) {
  if (!watchFolder || !watchFolder.library_folder) {
    return;
  }

  const folderId = normalizeFolderPath(watchFolder.library_folder);
  state.library.activeFolderId = folderId;
  state.library.previewDocumentId = null;
  state.library.editorDismissed = false;
  expandFolderAncestors(folderId);
  renderBrowserStats();
  renderLibraryExplorer();
  renderPreview();
  beginFolderInlineRename(folderId);
}

async function updateWatchedFolderAlias(watchFolder, nextAliasValue) {
  if (!watchFolder || !watchFolder.watch_id || state.library.watchFolderInFlight) {
    return false;
  }

  const currentAlias = watchFolder.alias || "";
  const normalizedNextAlias = String(nextAliasValue || "").trim();
  if (normalizedNextAlias === currentAlias) {
    setLibraryActionStatus("The watched folder alias is unchanged.");
    return true;
  }

  setWatchFolderState(true);
  setWatchFolderStatus("Updating watched folder alias...");
  try {
    const response = await fetch(`/api/watch-folders/${encodeURIComponent(watchFolder.watch_id)}`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        alias: normalizedNextAlias || null,
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      if (response.status === 405) {
        throw new Error("Alias updates require an app restart because the running server is using an older API version.");
      }
      throw new Error(getErrorMessageFromPayload(payload, "Update watched folder alias failed"));
    }
    await loadWatchedFolders();
    const message = payload.message || "Watched folder alias updated.";
    setWatchFolderStatus(message, "success");
    setLibraryActionStatus(message, "success");
    return true;
  } catch (error) {
    setWatchFolderStatus(error.message, "error");
    setLibraryActionStatus(error.message, "error");
    return false;
  } finally {
    setWatchFolderState(false);
  }
}

async function updateWatchedFolderSettings(watchFolder) {
  if (!watchFolder || !watchFolder.watch_id || state.library.watchFolderInFlight) {
    return false;
  }

  const intervalMinutes = Number.parseInt(folderPropertiesIntervalInput.value || "30", 10);
  if (!Number.isInteger(intervalMinutes) || intervalMinutes < 1 || intervalMinutes > 1440) {
    setLibraryActionStatus("Synchronization interval must be between 1 and 1440 minutes.", "error");
    folderPropertiesIntervalInput.focus();
    return false;
  }

  setWatchFolderState(true);
  setLibraryActionStatus("Saving synchronized folder settings...");
  try {
    const response = await fetch(`/api/watch-folders/${encodeURIComponent(watchFolder.watch_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        alias: String(folderPropertiesAliasInput.value || "").trim() || null,
        interval_minutes: intervalMinutes,
        category: String(folderPropertiesCategoryInput.value || "").trim() || "watched",
        tags: normalizeTagItems(folderPropertiesTagsInput.value || ""),
        recursive: Boolean(folderPropertiesRecursiveInput.checked),
        enabled: Boolean(folderPropertiesEnabledInput.checked),
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(getErrorMessageFromPayload(payload, "Update watched folder settings failed"));
    }
    await loadWatchedFolders();
    setLibraryActionStatus(payload.message || "Watched folder settings updated.", "success");
    return true;
  } catch (error) {
    setLibraryActionStatus(error.message, "error");
    return false;
  } finally {
    setWatchFolderState(false);
  }
}

async function syncWatchedFolder(watchId) {
  if (!watchId || state.library.watchFolderInFlight) {
    return;
  }
  setWatchFolderState(true);
  setWatchFolderStatus("Syncing watched folder...");
  try {
    const response = await fetch(`/api/watch-folders/${encodeURIComponent(watchId)}/sync`, {
      method: "POST",
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(getErrorMessageFromPayload(payload, "Watched folder sync failed"));
    }
    await loadWatchedFolders();
    await refreshLibraryAfterWatchSync(payload.results || []);
    setWatchFolderStatus(payload.message || "Watched folder synced.", "success");
  } catch (error) {
    setWatchFolderStatus(error.message, "error");
  } finally {
    setWatchFolderState(false);
  }
}

async function syncAllWatchedFolders() {
  if (state.library.watchFolderInFlight) {
    return;
  }
  setWatchFolderState(true);
  setWatchFolderStatus("Syncing all watched folders...");
  try {
    const response = await fetch("/api/watch-folders/sync", {
      method: "POST",
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(getErrorMessageFromPayload(payload, "Watched folder sync failed"));
    }
    await loadWatchedFolders();
    await refreshLibraryAfterWatchSync(payload.results || []);
    setWatchFolderStatus(payload.message || "Watched folders synced.", "success");
  } catch (error) {
    setWatchFolderStatus(error.message, "error");
  } finally {
    setWatchFolderState(false);
  }
}

async function deleteWatchedFolder(watchId) {
  if (!watchId || state.library.watchFolderInFlight) {
    return;
  }
  const watchedFolder = state.library.watchFolders.find(
    (item) => item.watch_id === watchId
  );
  const sourcePath = watchedFolder?.source_path || "this source folder";
  if (!window.confirm(
    `Unsynchronize ${sourcePath}? Existing embedded documents will remain in the library, ` +
    "and the source folder on disk will not be deleted."
  )) {
    return;
  }
  setWatchFolderState(true);
  setWatchFolderStatus("Unsynchronizing folder path...");
  try {
    const response = await fetch(`/api/watch-folders/${encodeURIComponent(watchId)}`, {
      method: "DELETE",
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(getErrorMessageFromPayload(payload, "Remove watched folder failed"));
    }
    await loadWatchedFolders();
    setWatchFolderStatus(payload.message || "Folder path unsynchronized.", "success");
  } catch (error) {
    setWatchFolderStatus(error.message, "error");
  } finally {
    setWatchFolderState(false);
  }
}

function setDocumentGenerationState(isGenerating) {
  state.generation.inFlight = isGenerating;
  [
    documentGenerationTitleInput,
    documentGenerationFormatSelect,
    documentGenerationInstructionsInput,
    generateDocumentButton,
  ].forEach((element) => {
    if (element) {
      element.disabled = isGenerating;
    }
  });
  if (generateDocumentButton) {
    generateDocumentButton.textContent = isGenerating ? "Generating..." : "Generate file";
  }
}

function setDocumentGenerationStatus(message, tone = "neutral") {
  if (!documentGenerationStatus) {
    return;
  }
  documentGenerationStatus.textContent = message;
  documentGenerationStatus.classList.remove("is-error", "is-success");
  if (tone === "error") {
    documentGenerationStatus.classList.add("is-error");
  }
  if (tone === "success") {
    documentGenerationStatus.classList.add("is-success");
  }
}

function setDeleteState(isDeleting) {
  state.library.deleteInFlight = isDeleting;
  renderDeleteSelectionSummary();
  renderFolderTree();
  renderDocumentFileList();
}

function setLibraryActionStatus(message, tone = "neutral") {
  libraryActionStatus.textContent = message;
  libraryActionStatus.classList.remove("is-error", "is-success");
  if (tone === "error") {
    libraryActionStatus.classList.add("is-error");
  }
  if (tone === "success") {
    libraryActionStatus.classList.add("is-success");
  }
}

function setDocumentEditorStatus(message, tone = "neutral") {
  documentEditorStatus.textContent = message;
  documentEditorStatus.classList.remove("is-error", "is-success");
  if (tone === "error") {
    documentEditorStatus.classList.add("is-error");
  }
  if (tone === "success") {
    documentEditorStatus.classList.add("is-success");
  }
}

function setDocumentEditorState(isUpdating) {
  state.library.metadataUpdateInFlight = isUpdating;
  const hasPreviewDocument = Boolean(getPreviewDocument());
  const canEdit = canMutateLibrary() && hasPreviewDocument;

  [
    documentEditorTitleInput,
    documentEditorCategoryInput,
    documentEditorFolderInput,
    documentEditorTagsInput,
    saveDocumentChangesButton,
    closeDocumentEditorButton,
  ].forEach((element) => {
    element.disabled = isUpdating || !canEdit;
  });
  saveDocumentChangesButton.textContent = isUpdating ? "Saving changes..." : "Save changes";
}

function closeDocumentEditor() {
  if (!getPreviewDocument() || state.library.metadataUpdateInFlight) {
    return;
  }

  state.library.editorDismissed = true;
  renderDocumentEditor();
}

function buildScopeLabel() {
  const { folderIds, documentIds } = state.library.appliedContext;
  if (folderIds.length === 0 && documentIds.length === 0) {
    return "Current scope: all indexed documents are available for retrieval.";
  }

  const parts = [];
  if (folderIds.length > 0) {
    parts.push(`${folderIds.length} folder${folderIds.length === 1 ? "" : "s"}`);
  }
  if (documentIds.length > 0) {
    parts.push(`${documentIds.length} document${documentIds.length === 1 ? "" : "s"}`);
  }
  return `Current scope: ${parts.join(" and ")} selected for retrieval.`;
}

function buildIntroMessage() {
  return `${modeCopy[state.sourceMode].intro}\n\n${buildScopeLabel()}`;
}

function renderContextSummary() {
  if (state.library.loadError) {
    contextSummary.textContent = "Document library could not be loaded.";
    contextChipList.innerHTML = "";
    return;
  }

  contextSummary.textContent =
    state.library.appliedContext.folderIds.length === 0 && state.library.appliedContext.documentIds.length === 0
      ? "All indexed documents are currently available for retrieval."
      : buildScopeLabel();

  contextChipList.innerHTML = "";

  state.library.appliedContext.folderIds.forEach((folderId) => {
    const folder = getFolderSummary(folderId);
    const chip = document.createElement("span");
    chip.className = "context-chip";
    chip.textContent = `Folder: ${folder ? folder.display_name : folderId}`;
    contextChipList.appendChild(chip);
  });

  state.library.appliedContext.documentIds.forEach((documentId) => {
    const docSummary = getDocumentSummary(documentId);
    const chip = document.createElement("span");
    chip.className = "context-chip";
    chip.textContent = docSummary ? getDocumentChipLabel(docSummary) : `Doc: ${documentId}`;
    contextChipList.appendChild(chip);
  });
}

function getScopeCoverage(context) {
  const includedDocuments = state.library.documents.filter((docSummary) => documentMatchesContext(docSummary, context));
  const excludedDocuments = state.library.documents.filter((docSummary) => !documentMatchesContext(docSummary, context));
  return {
    includedDocuments,
    excludedDocuments,
  };
}

function buildScopeStatusText(context, coverage, audienceLabel) {
  if (state.library.loadError) {
    return "Document library unavailable.";
  }

  if (context.folderIds.length === 0 && context.documentIds.length === 0) {
    return `${audienceLabel}: all ${state.library.totalDocuments} indexed document${state.library.totalDocuments === 1 ? "" : "s"} are available.`;
  }

  const parts = [];
  if (context.folderIds.length > 0) {
    parts.push(`${context.folderIds.length} folder${context.folderIds.length === 1 ? "" : "s"}`);
  }
  if (context.documentIds.length > 0) {
    parts.push(`${context.documentIds.length} direct document${context.documentIds.length === 1 ? "" : "s"}`);
  }

  return (
    `${audienceLabel}: ${coverage.includedDocuments.length} document${coverage.includedDocuments.length === 1 ? "" : "s"} included ` +
    `from ${parts.join(" and ")}. ${coverage.excludedDocuments.length} outside scope.`
  );
}

function buildExcludedFolderBreakdown(documents) {
  const counts = new Map();
  documents.forEach((docSummary) => {
    const folderId = normalizeFolderPath(docSummary.folder);
    counts.set(folderId, (counts.get(folderId) || 0) + 1);
  });

  return [...counts.entries()]
    .map(([folderId, count]) => ({
      folderId,
      label: `${formatFolderDisplayPath(folderId)} (${count})`,
      count,
    }))
    .sort((left, right) => {
      if (right.count !== left.count) {
        return right.count - left.count;
      }
      return left.folderId.localeCompare(right.folderId);
    });
}

function renderScopePill(container, label, onRemove) {
  const item = document.createElement("div");
  item.className = "scope-item";

  const text = document.createElement("span");
  text.className = "scope-item-label";
  text.textContent = label;
  item.appendChild(text);

  if (onRemove) {
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "scope-item-remove";
    removeButton.textContent = "Remove";
    removeButton.addEventListener("click", onRemove);
    item.appendChild(removeButton);
  }

  container.appendChild(item);
}

function renderScopePane() {
  if (state.library.loadError) {
    scopeInventorySummary.textContent = "Document library unavailable.";
    scopeAppliedSummary.textContent = state.library.loadError;
    scopeDraftSummary.textContent = state.library.loadError;
    scopeIncludedList.innerHTML = "";
    scopeExcludedList.innerHTML = "";
    return;
  }

  const appliedCoverage = getScopeCoverage(state.library.appliedContext);
  const draftCoverage = getScopeCoverage(state.library.draftContext);
  const draftChanged = !contextFiltersEqual(state.library.draftContext, state.library.appliedContext);

  scopeInventorySummary.textContent =
    draftCoverage.excludedDocuments.length === 0
      ? "Everything in the indexed library is currently in scope."
      : `${draftCoverage.includedDocuments.length} document${draftCoverage.includedDocuments.length === 1 ? "" : "s"} in scope, ${draftCoverage.excludedDocuments.length} outside scope.`;
  scopeAppliedSummary.textContent = buildScopeStatusText(state.library.appliedContext, appliedCoverage, "Applied");
  scopeDraftSummary.textContent = buildScopeStatusText(state.library.draftContext, draftCoverage, "Draft");
  if (draftChanged) {
    scopeDraftSummary.textContent += " Apply scope to use these browser selections in chat.";
  }

  scopeIncludedList.innerHTML = "";
  if (
    state.library.draftContext.folderIds.length === 0 &&
    state.library.draftContext.documentIds.length === 0
  ) {
    const empty = document.createElement("p");
    empty.className = "scope-list-empty";
    empty.textContent = "All indexed documents are included right now.";
    scopeIncludedList.appendChild(empty);
  } else {
    state.library.draftContext.folderIds.forEach((folderId) => {
      renderScopePill(
        scopeIncludedList,
        `Folder: ${formatFolderDisplayPath(folderId)}`,
        () => {
          toggleFolderScope(folderId);
        }
      );
    });

    state.library.draftContext.documentIds.forEach((documentId) => {
      const docSummary = getDocumentSummary(documentId);
      renderScopePill(
        scopeIncludedList,
        docSummary ? getDocumentChipLabel(docSummary) : `Doc: ${documentId}`,
        () => {
          toggleDocumentScope(documentId);
        }
      );
    });
  }

  scopeExcludedList.innerHTML = "";
  if (draftCoverage.excludedDocuments.length === 0) {
    const empty = document.createElement("p");
    empty.className = "scope-list-empty";
    empty.textContent = "Nothing is excluded from the current draft scope.";
    scopeExcludedList.appendChild(empty);
    return;
  }

  const excludedFolders = buildExcludedFolderBreakdown(draftCoverage.excludedDocuments);
  excludedFolders.slice(0, 8).forEach((folderEntry) => {
    renderScopePill(scopeExcludedList, folderEntry.label, null);
  });

  if (excludedFolders.length > 8) {
    const more = document.createElement("p");
    more.className = "scope-list-empty";
    more.textContent = `${excludedFolders.length - 8} more folder group${excludedFolders.length - 8 === 1 ? "" : "s"} remain outside scope.`;
    scopeExcludedList.appendChild(more);
  }
}

function renderDeleteSelectionSummary() {
  const selectedIds = state.library.deleteSelectionIds;

  if (selectedIds.length === 0) {
    deleteSelectionSummary.textContent = "No documents selected for deletion.";
    deleteChipList.innerHTML = "";
  } else {
    const noun = selectedIds.length === 1 ? "document" : "documents";
    deleteSelectionSummary.textContent = `${selectedIds.length} ${noun} selected for deletion.`;
    deleteChipList.innerHTML = "";
    selectedIds.forEach((documentId) => {
      const docSummary = getDocumentSummary(documentId);
      const chip = document.createElement("span");
      chip.className = "context-chip";
      chip.textContent = docSummary
        ? (hasGeneratedUploadId(docSummary.document_id) ? docSummary.title : docSummary.document_id)
        : documentId;
      deleteChipList.appendChild(chip);
    });
  }

  deleteSelectedButton.disabled =
    selectedIds.length === 0 ||
    state.library.deleteInFlight ||
    !canMutateLibrary();
  deleteSelectedButton.textContent = state.library.deleteInFlight ? "Deleting..." : "Delete selected";
}

function toggleDeleteSelection(documentId) {
  const nextIds = new Set(state.library.deleteSelectionIds);
  if (nextIds.has(documentId)) {
    nextIds.delete(documentId);
  } else {
    nextIds.add(documentId);
  }
  state.library.deleteSelectionIds = normalizeItems(Array.from(nextIds));
  renderBrowserStats();
  renderDeleteSelectionSummary();
  renderFolderTree();
  renderDocumentFileList();
}

async function loadHealth() {
  if (!statusPill) {
    return;
  }

  try {
    const response = await fetch("/api/health");
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error("Health check failed");
    }
    statusPill.textContent = payload.openai_configured
      ? `Ready - ${payload.docstore_backend} datastore`
      : "OpenAI key missing";
  } catch (error) {
    statusPill.textContent = "Server unavailable";
  }
}

function closeSavedConversationContextMenu() {
  state.memory.contextMenu = {
    open: false,
    conversationId: null,
    x: 0,
    y: 0,
  };
  if (!savedConversationContextMenu) {
    return;
  }
  savedConversationContextMenu.classList.add("is-hidden");
  savedConversationContextMenu.setAttribute("aria-hidden", "true");
  savedConversationContextMenu.style.left = "";
  savedConversationContextMenu.style.top = "";
}

function openSavedConversationContextMenu({ conversationId, x, y }) {
  if (!savedConversationContextMenu || !savedConversationRenameButton || !savedConversationDeleteButton) {
    return;
  }

  state.memory.contextMenu = {
    open: true,
    conversationId,
    x,
    y,
  };
  const contextDisabled = Boolean(state.memory.renameInFlightId || state.memory.deleteInFlightId || state.sending);
  savedConversationRenameButton.disabled = contextDisabled;
  savedConversationDeleteButton.disabled = contextDisabled;
  savedConversationContextMenu.classList.remove("is-hidden");
  savedConversationContextMenu.setAttribute("aria-hidden", "false");

  const menuWidth = Math.max(200, savedConversationContextMenu.offsetWidth);
  const menuHeight = savedConversationContextMenu.offsetHeight;
  const left = Math.max(12, Math.min(x, window.innerWidth - menuWidth - 12));
  const top = Math.max(12, Math.min(y, window.innerHeight - menuHeight - 12));
  savedConversationContextMenu.style.left = `${left}px`;
  savedConversationContextMenu.style.top = `${top}px`;
}

async function renameSavedConversation(conversationId) {
  const conversation = getSavedConversationSummary(conversationId);
  if (!conversation || state.memory.renameInFlightId || state.memory.deleteInFlightId) {
    return;
  }

  const currentLabel = getSavedConversationDisplayLabel(conversation);
  const nextTitle = window.prompt("Rename saved conversation", currentLabel);
  if (nextTitle === null) {
    return;
  }

  const normalizedTitle = nextTitle.trim();
  if (!normalizedTitle) {
    setConversationMemoryStatus("Saved conversation name cannot be empty.", "error");
    return;
  }

  state.memory.renameInFlightId = conversationId;
  renderSavedConversationList();
  renderConversationSaveButton();
  setConversationMemoryStatus(`Renaming "${currentLabel}"...`);

  try {
    const response = await fetch("/api/conversations/save", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        conversation_id: conversationId,
        title: normalizedTitle,
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Rename failed";
      throw new Error(detail);
    }

    state.memory.loaded = true;
    state.memory.loadError = null;
    upsertSavedConversationSummary(buildConversationSummary(payload.conversation));
    renderSavedConversationList();
    setConversationMemoryStatus(`Renamed saved conversation to "${normalizedTitle}".`, "success");
  } catch (error) {
    setConversationMemoryStatus(`Rename failed: ${error.message}`, "error");
  } finally {
    state.memory.renameInFlightId = null;
    closeSavedConversationContextMenu();
    renderSavedConversationList();
    renderConversationSaveButton();
  }
}

function renderConversationSaveButton() {
  if (!saveConversationButton) {
    return;
  }
  const currentSaved = Boolean(getSavedConversationSummary(state.conversationId));
  saveConversationButton.disabled =
    state.memory.saveInFlight ||
    Boolean(state.memory.renameInFlightId) ||
    state.sending ||
    !hasConversationMessages();
  saveConversationButton.textContent = state.memory.saveInFlight
    ? "Saving..."
    : currentSaved
      ? "Update saved"
      : "Save current";
}

function renderSavedConversationList() {
  savedConversationList.innerHTML = "";

  if (state.memory.loadError) {
    const error = document.createElement("p");
    error.className = "saved-conversation-empty";
    error.textContent = `Recents unavailable: ${state.memory.loadError}`;
    savedConversationList.appendChild(error);
    return;
  }

  if (!state.memory.loaded) {
    const loading = document.createElement("p");
    loading.className = "saved-conversation-empty";
    loading.textContent = "Loading recents...";
    savedConversationList.appendChild(loading);
    return;
  }

  if (state.memory.conversations.length === 0) {
    const empty = document.createElement("p");
    empty.className = "saved-conversation-empty";
    empty.textContent = "No recent chats yet.";
    savedConversationList.appendChild(empty);
    return;
  }

  const visibleConversations = getVisibleSavedConversations();
  if (visibleConversations.length === 0) {
    const empty = document.createElement("p");
    empty.className = "saved-conversation-empty";
    empty.textContent = "No recents match that search.";
    savedConversationList.appendChild(empty);
    return;
  }

  visibleConversations.forEach((conversation) => {
    const item = document.createElement("article");
    item.className = "saved-conversation-item";
    item.classList.toggle("is-active", conversation.conversation_id === state.conversationId);
    item.title = "Right-click to rename or delete this conversation.";
    item.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      openSavedConversationContextMenu({
        conversationId: conversation.conversation_id,
        x: event.clientX,
        y: event.clientY,
      });
    });

    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "saved-conversation-open";
    openButton.setAttribute("aria-label", `Open ${getSavedConversationDisplayLabel(conversation)}`);
    openButton.disabled =
      state.sending ||
      state.memory.renameInFlightId === conversation.conversation_id ||
      state.memory.deleteInFlightId === conversation.conversation_id;
    openButton.addEventListener("click", async () => {
      await openSavedConversation(conversation.conversation_id);
    });

    const title = document.createElement("span");
    title.className = "saved-conversation-title";
    title.textContent = getSavedConversationDisplayLabel(conversation);
    openButton.appendChild(title);

    if (state.memory.deleteInFlightId === conversation.conversation_id) {
      const meta = document.createElement("span");
      meta.className = "saved-conversation-meta";
      meta.textContent = "Deleting...";
      openButton.appendChild(meta);
    }

    item.appendChild(openButton);
    savedConversationList.appendChild(item);
  });
}

function setConversationMemoryStatus(message, tone = "neutral") {
  conversationMemoryStatus.textContent = message;
  conversationMemoryStatus.classList.remove("is-error", "is-success");
  if (tone === "error") {
    conversationMemoryStatus.classList.add("is-error");
  }
  if (tone === "success") {
    conversationMemoryStatus.classList.add("is-success");
  }
}

function updateConversationMemoryStatus() {
  if (state.memory.loadError) {
    setConversationMemoryStatus(`Recents unavailable: ${state.memory.loadError}`, "error");
    return;
  }

  if (state.memory.saveInFlight) {
    setConversationMemoryStatus("Autosaving conversation...");
    return;
  }

  if (getSavedConversationSummary(state.conversationId)) {
    setConversationMemoryStatus(
      "Autosaved. New replies will keep this recent updated.",
      "success"
    );
    return;
  }

  if (hasConversationMessages()) {
    setConversationMemoryStatus("This chat will autosave after the assistant replies.");
    return;
  }

  setConversationMemoryStatus("Conversations save automatically after each assistant reply.");
}

function applySourceMode(sourceMode) {
  state.sourceMode = sourceMode;

  modeButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.sourceMode === sourceMode);
  });

  modeDescription.textContent = modeCopy[sourceMode].description;
  composerNote.textContent = modeCopy[sourceMode].composerNote;
}

function applyConversationPreferences(preferences) {
  const normalized = normalizeConversationPreferences(preferences);
  applySourceMode(normalized.sourceMode);
  applyContextSelection(normalized.contextFilter);
}

function renderIntroMessage() {
  renderMessage(
    {
      role: "assistant",
      label: "Assistant",
      body: buildIntroMessage(),
    },
    { persist: false, scrollToLatest: false }
  );
  messageList.scrollTop = 0;
}

function renderConversationMessages(messages) {
  state.messages = [];
  state.responseIndicatorNode = null;
  messageList.innerHTML = "";

  if (!Array.isArray(messages) || messages.length === 0) {
    renderIntroMessage();
    return;
  }

  messages.forEach((message) => {
    renderMessage(message, { scrollToLatest: false });
  });
  messageList.scrollTop = 0;
}

function resetConversation() {
  state.conversationId = crypto.randomUUID();
  applyConversationPreferences(getLastUsedConversationPreferences());
  rememberConversationPreferences(state.conversationId);
  renderConversationMessages([]);
  renderSavedConversationList();
  renderConversationSaveButton();
  updateConversationMemoryStatus();
}

async function loadSavedConversations() {
  try {
    const response = await fetch("/api/conversations");
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Saved conversations request failed";
      throw new Error(detail);
    }

    state.memory.conversations = sortSavedConversations(
      (payload.conversations || []).map((conversation) => buildConversationSummary(conversation))
    );
    state.memory.loaded = true;
    state.memory.loadError = null;
  } catch (error) {
    state.memory.conversations = [];
    state.memory.loaded = true;
    state.memory.loadError = error.message;
  }

  renderSavedConversationList();
  renderConversationSaveButton();
  updateConversationMemoryStatus();
}

async function openSavedConversation(conversationId) {
  if (!conversationId || state.sending) {
    return;
  }

  setConversationMemoryStatus("Loading saved conversation...");

  try {
    const response = await fetch(`/api/conversations/${encodeURIComponent(conversationId)}`);
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Saved conversation request failed";
      throw new Error(detail);
    }

    state.conversationId = payload.conversation_id;
    upsertSavedConversationSummary(buildConversationSummary(payload));
    const preferences = resolveSavedConversationPreferences(payload);
    const serverUpdatedAt = Date.parse(payload.updated_at || "") || 0;
    const shouldSyncLocalPreferences = preferences.updatedAt > serverUpdatedAt;
    applyConversationPreferences(preferences);
    const rememberedPreferences = rememberConversationPreferences(state.conversationId, preferences);
    if (shouldSyncLocalPreferences) {
      void syncConversationPreferences(state.conversationId, rememberedPreferences);
    }
    renderConversationMessages(payload.messages || []);
    renderSavedConversationList();
    renderConversationSaveButton();
    setConversationMemoryStatus(
      `Opened "${getSavedConversationDisplayLabel(buildConversationSummary(payload))}".`,
      "success"
    );
  } catch (error) {
    setConversationMemoryStatus(`Open failed: ${error.message}`, "error");
  }
}

async function saveCurrentConversation(options = {}) {
  const { silent = false } = options;

  if (state.memory.saveInFlight) {
    return null;
  }

  if (!hasConversationMessages()) {
    if (!silent) {
      setConversationMemoryStatus("There is nothing to save yet.");
    }
    return null;
  }

  state.memory.saveInFlight = true;
  renderConversationSaveButton();
  if (!silent) {
    setConversationMemoryStatus("Saving current conversation...");
  } else {
    updateConversationMemoryStatus();
  }

  try {
    const response = await fetch("/api/conversations/save", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        conversation_id: state.conversationId,
        source_mode: state.sourceMode,
        context_filter: {
          folder_ids: state.library.appliedContext.folderIds,
          document_ids: state.library.appliedContext.documentIds,
        },
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Save failed";
      throw new Error(detail);
    }

    state.memory.loaded = true;
    state.memory.loadError = null;
    rememberConversationPreferences(state.conversationId);
    upsertSavedConversationSummary(buildConversationSummary(payload.conversation));
    renderSavedConversationList();
    if (silent) {
      updateConversationMemoryStatus();
    } else {
      setConversationMemoryStatus("Conversation saved. It now appears in the sidebar.", "success");
    }
    return payload.conversation;
  } catch (error) {
    setConversationMemoryStatus(
      silent ? `Saved conversation sync failed: ${error.message}` : `Save failed: ${error.message}`,
      "error"
    );
    return null;
  } finally {
    state.memory.saveInFlight = false;
    renderConversationSaveButton();
  }
}

async function deleteSavedConversation(conversationId) {
  if (!conversationId || state.memory.deleteInFlightId) {
    return;
  }

  closeSavedConversationContextMenu();
  state.memory.deleteInFlightId = conversationId;
  renderSavedConversationList();
  setConversationMemoryStatus("Deleting saved conversation...");

  try {
    const response = await fetch(`/api/conversations/${encodeURIComponent(conversationId)}`, {
      method: "DELETE",
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Delete failed";
      throw new Error(detail);
    }

    state.memory.conversations = state.memory.conversations.filter(
      (item) => item.conversation_id !== conversationId
    );
    const pendingPreferenceSync = conversationPreferenceSyncTimers.get(conversationId);
    if (pendingPreferenceSync) {
      window.clearTimeout(pendingPreferenceSync);
      conversationPreferenceSyncTimers.delete(conversationId);
    }
    if (conversationId !== state.conversationId) {
      forgetConversationPreferences(conversationId);
    }
    renderSavedConversationList();
    renderConversationSaveButton();
    if (conversationId === state.conversationId) {
      setConversationMemoryStatus(
        "Saved conversation deleted. The current chat stays open until you start a new one.",
        "success"
      );
    } else {
      setConversationMemoryStatus("Saved conversation deleted.", "success");
    }
  } catch (error) {
    setConversationMemoryStatus(`Delete failed: ${error.message}`, "error");
  } finally {
    state.memory.deleteInFlightId = null;
    renderSavedConversationList();
    renderConversationSaveButton();
  }
}

async function loadDocumentLibrary() {
  try {
    if (!state.library.loaded) {
      browserStats.textContent = "Loading library...";
    }
    const response = await fetch("/api/documents");
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Document library request failed";
      throw new Error(detail);
    }

    state.library.backend = payload.backend;
    state.library.totalDocuments = payload.total_documents;
    state.library.totalChunks = payload.total_chunks;
    state.library.folders = payload.folders || [];
    state.library.documents = payload.documents || [];
    state.library.loaded = true;
    state.library.loadError = null;
    reconcileLibraryState();
    if (state.library.collapseFoldersOnLoad) {
      state.library.collapseFoldersOnLoad = false;
      state.library.collapsedFolderIds = getAllLibraryFolderPathIds();
    }
    renderContextSummary();
    renderBrowserStats();
    renderDeleteSelectionSummary();
    renderLibraryExplorer();
  } catch (error) {
    state.library.collapseFoldersOnLoad = false;
    state.library.loadError = error.message;
    contextSummary.textContent = "Document library could not be loaded.";
    browserStats.textContent = `Library unavailable: ${error.message}`;
    deleteSelectionSummary.textContent = "Delete controls are unavailable because the library could not be loaded.";
    deleteChipList.innerHTML = "";
    deleteSelectedButton.disabled = true;
    renderLibraryExplorer();
    setLibraryActionStatus(`Library unavailable: ${error.message}`, "error");
  }
}

function renderBrowserStats() {
  if (state.library.loadError) {
    browserStats.textContent = `Library unavailable: ${state.library.loadError}`;
    return;
  }

  const visibleDocuments = getVisibleDocuments().length;
  const folderCount = state.library.folders.length;
  const totalChunks =
    state.library.totalChunks === null || state.library.totalChunks === undefined
      ? null
      : state.library.totalChunks;

  const baseParts = [
    `${state.library.totalDocuments} docs`,
    `${folderCount} folder${folderCount === 1 ? "" : "s"}`,
  ];
  if (totalChunks !== null) {
    baseParts.push(`${totalChunks} chunks`);
  }
  baseParts.push(
    state.library.activeFolderId
      ? `view: ${formatFolderDisplayPath(state.library.activeFolderId)}`
      : "view: all documents"
  );
  baseParts.push(`${visibleDocuments} visible`);
  if (state.library.searchQuery.trim()) {
    baseParts.push(`filter: "${state.library.searchQuery.trim()}"`);
  }

  const draft = state.library.draftContext;
  if (draft.folderIds.length === 0 && draft.documentIds.length === 0) {
    baseParts.push("draft scope: all docs");
  } else {
    baseParts.push(
      `draft scope: ${draft.folderIds.length} folder${draft.folderIds.length === 1 ? "" : "s"}, ` +
      `${draft.documentIds.length} doc${draft.documentIds.length === 1 ? "" : "s"}`
    );
  }
  if (state.library.deleteSelectionIds.length > 0) {
    baseParts.push(
      `delete: ${state.library.deleteSelectionIds.length} selected`
    );
  }

  browserStats.textContent = baseParts.join(" - ");
}

function renderLibraryBreadcrumbs() {
  libraryBreadcrumbs.innerHTML = "";

  if (state.library.loadError) {
    const error = document.createElement("p");
    error.className = "mode-description";
    error.textContent = state.library.loadError;
    libraryBreadcrumbs.appendChild(error);
    return;
  }

  const rootButton = document.createElement("button");
  rootButton.type = "button";
  rootButton.className = "breadcrumb-button";
  rootButton.classList.toggle("is-active", !state.library.activeFolderId);
  rootButton.textContent = "All documents";
  rootButton.addEventListener("click", () => {
    setActiveFolder(null);
  });
  libraryBreadcrumbs.appendChild(rootButton);

  if (!state.library.activeFolderId) {
    return;
  }

  const segments = state.library.activeFolderId.split("/");
  segments.forEach((segment, index) => {
    const separator = document.createElement("span");
    separator.className = "breadcrumb-separator";
    separator.textContent = "/";
    libraryBreadcrumbs.appendChild(separator);

    const pathId = segments.slice(0, index + 1).join("/");
    const crumb = document.createElement("button");
    crumb.type = "button";
    crumb.className = "breadcrumb-button";
    crumb.classList.toggle("is-active", index === segments.length - 1);
    crumb.textContent = getFolderDisplayName(pathId, segment);
    crumb.addEventListener("click", () => {
      setActiveFolder(pathId);
    });
    libraryBreadcrumbs.appendChild(crumb);
  });
}

function buildDocumentMetaLine(docSummary) {
  const parts = [];
  if (!state.library.activeFolderId) {
    parts.push(formatFolderDisplayPath(docSummary.folder));
  }
  parts.push(docSummary.category);
  if (docSummary.tags && docSummary.tags.length > 0) {
    parts.push(docSummary.tags.slice(0, 2).join(", "));
  }
  if (!hasGeneratedUploadId(docSummary.document_id)) {
    parts.push(docSummary.document_id);
  }
  return parts.join(" - ");
}

function toggleFolderScope(folderId) {
  const nextFolders = new Set(state.library.draftContext.folderIds);
  if (nextFolders.has(folderId)) {
    nextFolders.delete(folderId);
  } else {
    nextFolders.add(folderId);
  }
  state.library.draftContext.folderIds = normalizeItems(Array.from(nextFolders));
  renderBrowserStats();
  renderScopePane();
  renderFolderTree();
  renderDocumentFileList();
}

function toggleDocumentScope(documentId) {
  const nextDocumentIds = new Set(state.library.draftContext.documentIds);
  if (nextDocumentIds.has(documentId)) {
    nextDocumentIds.delete(documentId);
  } else {
    nextDocumentIds.add(documentId);
  }
  state.library.draftContext.documentIds = normalizeItems(Array.from(nextDocumentIds));
  renderBrowserStats();
  renderScopePane();
  renderFolderTree();
  renderDocumentFileList();
}

function renderFolderTree() {
  folderTreeList.innerHTML = "";
  librarySearchInput.value = state.library.searchQuery;

  if (state.library.loadError) {
    const error = document.createElement("p");
    error.className = "mode-description";
    error.textContent = state.library.loadError;
    folderTreeList.appendChild(error);
    return;
  }

  const tree = getFolderTreeNodes();

  const bindFolderDropTarget = (element, targetFolderId, highlightTarget = element) => {
    element.addEventListener("dragover", (event) => {
      const payload = getExplorerDragData(event);
      const canDrop =
        isValidFolderDropTarget(payload, targetFolderId) ||
        isValidDocumentDropTarget(payload, targetFolderId);
      if (!canDrop) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = "move";
      highlightTarget.classList.add("is-drop-target");
    });

    element.addEventListener("dragleave", (event) => {
      if (!element.contains(event.relatedTarget)) {
        highlightTarget.classList.remove("is-drop-target");
      }
    });

    element.addEventListener("drop", async (event) => {
      const payload = getExplorerDragData(event);
      const canDropFolder = isValidFolderDropTarget(payload, targetFolderId);
      const canDropDocument = isValidDocumentDropTarget(payload, targetFolderId);
      if (!canDropFolder && !canDropDocument) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      clearDropTargetStyles();
      if (canDropFolder) {
        await moveFolderToFolder(payload.folderId, targetFolderId);
        return;
      }
      if (canDropDocument) {
        await moveDocumentToFolder(payload.documentId, targetFolderId);
      }
    });
  };

  bindFolderDropTarget(folderTreeList, state.library.activeFolderId);

  const createTreeDocumentRow = (docSummary, depth) => {
    const row = document.createElement("div");
    row.className = "folder-tree-document-row";
    if (state.library.previewDocumentId === docSummary.document_id) {
      row.classList.add("is-previewing");
    }
    if (state.library.deleteSelectionIds.includes(docSummary.document_id)) {
      row.classList.add("is-delete-selected");
    }
    row.dataset.documentId = docSummary.document_id;
    bindFolderDropTarget(row, normalizeFolderPath(docSummary.folder), row);

    row.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      event.stopPropagation();
      openExplorerContextMenu({
        targetType: "document",
        targetId: docSummary.document_id,
        label: docSummary.title || getDocumentDisplayLabel(docSummary),
        x: event.clientX,
        y: event.clientY,
      });
    });

    const button = document.createElement("button");
    button.type = "button";
    button.className = "folder-tree-document-entry";
    button.style.paddingLeft = `${depth * 12 + 28}px`;
    button.addEventListener("click", () => {
      void openDocumentPreview(docSummary.document_id);
    });
    button.title = getDocumentDisplayLabel(docSummary);
    bindFolderDropTarget(button, normalizeFolderPath(docSummary.folder), row);

    const label = document.createElement("span");
    label.className = "folder-tree-document-name";
    label.textContent = docSummary.title || getDocumentDisplayLabel(docSummary);
    button.appendChild(label);

    const category = document.createElement("span");
    category.className = "document-category-badge";
    category.textContent = docSummary.category || "Uncategorized";
    category.title = `Category: ${docSummary.category || "Uncategorized"}`;
    button.appendChild(category);

    bindExplorerDragSource(
      button,
      {
        type: "document",
        documentId: docSummary.document_id,
        currentFolderId: normalizeFolderPath(docSummary.folder),
      },
      row,
    );

    row.appendChild(button);

    const actions = document.createElement("div");
    actions.className = "folder-tree-document-actions";

    const scopeButton = document.createElement("button");
    scopeButton.type = "button";
    scopeButton.className = "scope-toggle-button";
    const includedViaFolder = state.library.draftContext.folderIds.some((folderId) =>
      folderPathContainsFolder(folderId, docSummary.folder)
    );
    const isScopedDirectly = state.library.draftContext.documentIds.includes(docSummary.document_id);
    scopeButton.classList.toggle("is-active", includedViaFolder || isScopedDirectly);
    scopeButton.disabled = includedViaFolder;
    scopeButton.textContent = includedViaFolder
      ? "Folder"
      : isScopedDirectly
        ? "Scoped"
        : "Scope";
    scopeButton.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleDocumentScope(docSummary.document_id);
    });
    actions.appendChild(scopeButton);

    const deleteSelectButton = document.createElement("button");
    deleteSelectButton.type = "button";
    deleteSelectButton.className = "delete-select-button";
    const isDeleteSelected = state.library.deleteSelectionIds.includes(docSummary.document_id);
    if (isDeleteSelected) {
      deleteSelectButton.classList.add("is-selected");
    }
    deleteSelectButton.textContent = isDeleteSelected ? "Marked" : "Delete";
    deleteSelectButton.disabled = state.library.deleteInFlight || !canMutateLibrary();
    deleteSelectButton.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleDeleteSelection(docSummary.document_id);
    });
    actions.appendChild(deleteSelectButton);

    row.appendChild(actions);
    return row;
  };

  const renderTreeNode = (node) => {
    const item = document.createElement("div");
    item.className = "folder-tree-item";
    const folderDocuments = getFolderDocuments(node.pathId);
    const canExpandNode = node.pathId !== null && (node.children.length > 0 || folderDocuments.length > 0);

    const row = document.createElement("div");
    row.className = "folder-tree-row";
    row.classList.toggle("is-active", node.pathId === state.library.activeFolderId);
    if (node.pathId === null && state.library.activeFolderId === null) {
      row.classList.add("is-active");
    }
    row.dataset.folderId = node.pathId || "";
    item.appendChild(row);
    bindFolderDropTarget(row, node.pathId, row);

    const main = document.createElement("div");
    main.className = "folder-tree-main";
    main.style.paddingLeft = `${node.depth * 12}px`;
    row.appendChild(main);

    if (canExpandNode) {
      const expandButton = document.createElement("button");
      expandButton.type = "button";
      expandButton.className = "folder-expand-button";
      expandButton.textContent = isFolderCollapsed(node.pathId) ? ">" : "v";
      expandButton.setAttribute("aria-label", isFolderCollapsed(node.pathId) ? "Expand folder" : "Collapse folder");
      expandButton.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleFolderCollapsed(node.pathId);
      });
      main.appendChild(expandButton);
    } else {
      const spacer = document.createElement("span");
      spacer.className = "folder-expand-spacer";
      spacer.textContent = "";
      main.appendChild(spacer);
    }

    const isInlineRenaming = Boolean(
      node.pathId && state.library.inlineRenameFolderId === normalizeFolderPath(node.pathId)
    );
    const openButton = document.createElement(isInlineRenaming ? "div" : "button");
    if (!isInlineRenaming) {
      openButton.type = "button";
    }
    openButton.className = "folder-tree-entry";
    openButton.classList.toggle("is-renaming", isInlineRenaming);
    if (!isInlineRenaming) {
      openButton.addEventListener("click", () => {
        setActiveFolder(node.pathId);
      });
      bindFolderDropTarget(openButton, node.pathId, row);
    }

    const icon = document.createElement("span");
    icon.className = "folder-tree-icon";
    icon.textContent = "•";
    openButton.appendChild(icon);

    const copy = document.createElement("span");
    copy.className = "folder-tree-copy";
    const name = document.createElement(isInlineRenaming ? "input" : "span");
    if (isInlineRenaming) {
      name.type = "text";
      name.className = "folder-tree-rename-input";
      name.value = state.library.inlineRenameDraft;
      name.maxLength = 160;
      name.disabled = state.library.inlineRenameInFlight;
      name.setAttribute("aria-label", `Rename ${getFolderDisplayName(node.pathId, node.label)}`);
      let renameFinished = false;
      const finishRename = async () => {
        if (renameFinished) {
          return;
        }
        renameFinished = true;
        await commitFolderInlineRename(node.pathId, name.value);
      };
      name.addEventListener("input", () => {
        state.library.inlineRenameDraft = name.value;
      });
      name.addEventListener("click", (event) => {
        event.stopPropagation();
      });
      name.addEventListener("pointerdown", (event) => {
        event.stopPropagation();
      });
      name.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          event.stopPropagation();
          void finishRename();
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          renameFinished = true;
          cancelFolderInlineRename();
        }
      });
      name.addEventListener("blur", () => {
        void finishRename();
      });
    } else {
      name.className = "folder-tree-name";
      name.textContent = node.pathId ? getFolderDisplayName(node.pathId, node.label) : node.label;
    }
    openButton.title = node.pathId
      ? `${formatFolderDisplayPath(node.pathId)} (${formatFolderPath(node.pathId)})`
      : "All documents";
    const count = document.createElement("span");
    count.className = "folder-tree-count";
    count.textContent = `${node.totalDocumentCount} doc${node.totalDocumentCount === 1 ? "" : "s"}`;
    copy.appendChild(name);
    copy.appendChild(count);
    openButton.appendChild(copy);
    main.appendChild(openButton);

    if (node.folder) {
      if (canMutateLibrary() && node.pathId !== null && !isInlineRenaming) {
        bindExplorerDragSource(
          openButton,
          {
            type: "folder",
            folderId: node.folder.folder_id,
          },
          row,
        );
      }

      row.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openExplorerContextMenu({
          targetType: "folder",
          targetId: node.folder.folder_id,
          label: getFolderDisplayName(node.pathId, node.label),
          x: event.clientX,
          y: event.clientY,
        });
      });

      const folderActions = document.createElement("div");
      folderActions.className = "folder-tree-folder-actions";

      const watchedFolder = getWatchedFolderForLibraryFolder(node.folder.folder_id);
      if (watchedFolder) {
        const syncButton = document.createElement("button");
        syncButton.type = "button";
        syncButton.className = "tree-action-button folder-sync-button";
        syncButton.textContent = state.library.watchFolderInFlight ? "Syncing" : "Sync";
        syncButton.disabled = state.library.watchFolderInFlight || isInlineRenaming;
        syncButton.title = `Sync ${getFolderDisplayName(node.pathId, node.label)}`;
        syncButton.addEventListener("click", async (event) => {
          event.stopPropagation();
          await syncWatchedFolder(watchedFolder.watch_id);
        });
        folderActions.appendChild(syncButton);
      }

      const action = document.createElement("button");
      action.type = "button";
      action.className = "tree-action-button";
      const isScoped = state.library.draftContext.folderIds.includes(node.folder.folder_id);
      const isIncludedViaAncestor = !isScoped && state.library.draftContext.folderIds.some((folderId) =>
        folderPathContainsFolder(folderId, node.folder.folder_id)
      );
      action.classList.toggle("is-active", isScoped || isIncludedViaAncestor);
      action.disabled = isIncludedViaAncestor;
      action.textContent = isScoped ? "Scoped" : isIncludedViaAncestor ? "Parent" : "Scope";
      action.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleFolderScope(node.folder.folder_id);
      });
      folderActions.appendChild(action);
      row.appendChild(folderActions);
    }

    if ((node.children.length > 0 || folderDocuments.length > 0) && (node.pathId === null || !isFolderCollapsed(node.pathId))) {
      const children = document.createElement("div");
      children.className = "folder-tree-children";
      node.children.forEach((child) => {
        children.appendChild(renderTreeNode(child));
      });
      folderDocuments.forEach((docSummary) => {
        children.appendChild(createTreeDocumentRow(docSummary, node.depth + 1));
      });
      item.appendChild(children);
    }

    return item;
  };

  folderTreeList.appendChild(renderTreeNode(tree));
}

function renderDocumentFileList() {
  if (!documentFileList) {
    return;
  }
  documentFileList.innerHTML = "";
  librarySearchInput.value = state.library.searchQuery;

  if (state.library.loadError) {
    const error = document.createElement("p");
    error.className = "mode-description";
    error.textContent = state.library.loadError;
    documentFileList.appendChild(error);
    return;
  }

  const visibleDocuments = getVisibleDocuments();

  const summary = document.createElement("div");
  summary.className = "document-file-list-summary";
  summary.textContent = state.library.activeFolderId
    ? `${visibleDocuments.length} file${visibleDocuments.length === 1 ? "" : "s"} in ${formatFolderDisplayPath(state.library.activeFolderId)}`
    : `${visibleDocuments.length} file${visibleDocuments.length === 1 ? "" : "s"} total`;
  documentFileList.appendChild(summary);

  if (visibleDocuments.length === 0) {
    const empty = document.createElement("div");
    empty.className = "document-file-empty";
    empty.textContent = state.library.searchQuery.trim()
      ? "No files matched this filter in the current folder view."
      : "No files are available in this folder view.";
    documentFileList.appendChild(empty);
    return;
  }

  visibleDocuments.forEach((docSummary) => {
    const row = document.createElement("article");
    row.className = "document-file-row";
    row.dataset.documentId = docSummary.document_id;
    if (state.library.previewDocumentId === docSummary.document_id) {
      row.classList.add("is-previewing");
    }
    if (state.library.deleteSelectionIds.includes(docSummary.document_id)) {
      row.classList.add("is-delete-selected");
    }
    row.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      openExplorerContextMenu({
        targetType: "document",
        targetId: docSummary.document_id,
        label: docSummary.title || getDocumentDisplayLabel(docSummary),
        x: event.clientX,
        y: event.clientY,
      });
    });
    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "document-file-main";
    openButton.addEventListener("click", () => {
      void openDocumentPreview(docSummary.document_id);
    });

    const header = document.createElement("div");
    header.className = "document-file-header";
    const title = document.createElement("span");
    title.className = "document-title";
    title.textContent = docSummary.title || getDocumentDisplayLabel(docSummary);
    header.appendChild(title);
    const category = document.createElement("span");
    category.className = "document-category-badge";
    category.textContent = docSummary.category || "Uncategorized";
    category.title = `Category: ${docSummary.category || "Uncategorized"}`;
    header.appendChild(category);
    openButton.appendChild(header);
    openButton.title = getDocumentDisplayLabel(docSummary);

    const meta = document.createElement("span");
    meta.className = "document-meta";
    meta.textContent = buildDocumentMetaLine(docSummary);
    openButton.appendChild(meta);

    bindExplorerDragSource(
      openButton,
      {
        type: "document",
        documentId: docSummary.document_id,
        currentFolderId: normalizeFolderPath(docSummary.folder),
      },
      row,
    );

    row.appendChild(openButton);

    const actions = document.createElement("div");
    actions.className = "document-actions";

    const scopeButton = document.createElement("button");
    scopeButton.type = "button";
    scopeButton.className = "scope-toggle-button";
    const includedViaFolder = state.library.draftContext.folderIds.some((folderId) =>
      folderPathContainsFolder(folderId, docSummary.folder)
    );
    const isScopedDirectly = state.library.draftContext.documentIds.includes(docSummary.document_id);
    scopeButton.classList.toggle("is-active", includedViaFolder || isScopedDirectly);
    scopeButton.disabled = includedViaFolder;
    scopeButton.textContent = includedViaFolder
      ? "Folder"
      : isScopedDirectly
        ? "Scoped"
        : "Scope";
    scopeButton.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleDocumentScope(docSummary.document_id);
    });
    actions.appendChild(scopeButton);

    const deleteSelectButton = document.createElement("button");
    deleteSelectButton.type = "button";
    deleteSelectButton.className = "delete-select-button";
    const isDeleteSelected = state.library.deleteSelectionIds.includes(docSummary.document_id);
    if (isDeleteSelected) {
      deleteSelectButton.classList.add("is-selected");
    }
    deleteSelectButton.textContent = isDeleteSelected ? "Marked" : "Delete";
    deleteSelectButton.disabled = state.library.deleteInFlight || !canMutateLibrary();
    deleteSelectButton.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleDeleteSelection(docSummary.document_id);
    });
    actions.appendChild(deleteSelectButton);

    row.appendChild(actions);
    documentFileList.appendChild(row);
  });
}

function renderLibraryExplorer() {
  renderScopePane();
  explorerRevealSelectionButton.disabled = !state.library.previewDocumentId;
  librarySearchInput.value = state.library.searchQuery;
  renderFolderTree();
  renderLibraryBreadcrumbs();
  renderDocumentFileList();
  renderDocumentEditor();
}

async function openDocumentPreview(documentId, options = {}) {
  const { revealInExplorer = false } = options;
  try {
    if (!state.library.previewCache[documentId]) {
      const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}`);
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        const detail = payload && payload.detail ? payload.detail : "Preview request failed";
        throw new Error(detail);
      }
      state.library.previewCache[documentId] = payload;
    }

    if (revealInExplorer) {
      const docSummary = getDocumentSummary(documentId);
      if (docSummary) {
        state.library.activeFolderId = normalizeFolderPath(docSummary.folder);
      }
      state.library.searchQuery = "";
    }

    state.library.previewDocumentId = documentId;
    state.library.editorDismissed = false;
    closeExplorerContextMenu();
    renderBrowserStats();
    renderLibraryExplorer();
    renderPreview();
  } catch (error) {
    previewEmpty.textContent = `Preview failed: ${error.message}`;
    previewEmpty.classList.remove("is-hidden");
    previewCard.classList.add("is-hidden");
  }
}

async function uploadDocument(event) {
  event.preventDefault();

  if (!canMutateLibrary()) {
    setUploadStatus("Uploads are only available for local json and semantic libraries.", "error");
    return;
  }

  const selectedFiles = [
    ...(uploadDirectoryInput.files ? Array.from(uploadDirectoryInput.files) : []),
    ...(uploadFileInput.files ? Array.from(uploadFileInput.files) : []),
  ];
  if (selectedFiles.length === 0) {
    setUploadStatus("Choose files or pick a folder to import.", "error");
    return;
  }

  const category = uploadCategoryInput.value.trim();
  const folder = uploadFolderInput.value.trim();
  const title = uploadTitleInput.value.trim();
  const tags = normalizeTagItems(uploadTagsInput.value.trim());

  setUploadState(true);
  setUploadStatus("Importing files into the library and rebuilding embeddings once...");

  try {
    const uploadBatch = await buildUploadPayloads(selectedFiles, {
      category,
      folderBase: folder,
      titleOverride: title,
      tags,
    });
    let payload = null;
    const uploadResolutions = {};

    while (true) {
      const response = await fetch("/api/documents/upload-batch", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          documents: applyUploadSimilarityResolutions(uploadBatch.documents, uploadResolutions, "warn"),
        }),
      });
      payload = await parseJsonResponse(response);
      if (response.status === 409 && payload && payload.detail && payload.detail.code === "similar_document_conflict") {
        Object.assign(uploadResolutions, collectSimilarUploadResolutions(payload.detail.conflicts || []));
        continue;
      }
      if (!response.ok) {
        const detail = payload && payload.detail
          ? (typeof payload.detail === "string" ? payload.detail : payload.detail.message || "Upload failed")
          : "Upload failed";
        throw new Error(detail);
      }
      break;
    }

    state.library.previewCache = {};
    state.library.previewDocumentId = null;
    await loadDocumentLibrary();

    if (payload.uploaded_documents && payload.uploaded_documents.length === 1) {
      await openDocumentPreview(payload.uploaded_documents[0].document_id, { revealInExplorer: true });
    } else {
      renderPreview();
    }

    uploadForm.reset();
    uploadDirectoryInput.value = "";
    const skippedMessage = uploadBatch.skippedCount > 0
      ? ` Skipped ${uploadBatch.skippedCount} unsupported file${uploadBatch.skippedCount === 1 ? "" : "s"}.`
      : "";
    setUploadStatus(`${payload.message || "Document import completed."}${skippedMessage}`, "success");
    setLibraryActionStatus("Library refreshed with the imported documents.", "success");
  } catch (error) {
    setUploadStatus(error.message, "error");
  } finally {
    setUploadState(false);
  }
}

async function deleteSelectedDocuments() {
  if (!canMutateLibrary()) {
    setLibraryActionStatus("Deletion is only available for local json and semantic libraries.", "error");
    return;
  }

  const selectedIds = [...state.library.deleteSelectionIds];
  if (selectedIds.length === 0 || state.library.deleteInFlight) {
    return;
  }

  const noun = selectedIds.length === 1 ? "document" : "documents";
  const confirmMessage =
    selectedIds.length === 1
      ? `Delete ${selectedIds[0]} from the local library and rebuild embeddings?`
      : `Delete ${selectedIds.length} selected ${noun} from the local library and rebuild embeddings?`;
  if (!window.confirm(confirmMessage)) {
    return;
  }

  setDeleteState(true);
  setLibraryActionStatus("Deleting selected documents and refreshing the embedded library...");

  try {
    const response = await fetch("/api/documents/delete", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        document_ids: selectedIds,
      }),
    });
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        const detail = payload && payload.detail ? payload.detail : "Delete failed";
        throw new Error(detail);
      }

    state.library.previewCache = {};
    if (selectedIds.includes(state.library.previewDocumentId)) {
      state.library.previewDocumentId = null;
    }
    state.library.deleteSelectionIds = [];
    await loadDocumentLibrary();
    renderPreview();
    setLibraryActionStatus(payload.message || "Selected documents were deleted.", "success");
  } catch (error) {
    setLibraryActionStatus(error.message, "error");
  } finally {
    setDeleteState(false);
  }
}

function hydrateDocumentEditor(previewDoc) {
  if (!previewDoc) {
    state.library.editorDocumentId = null;
    state.library.editorDirty = false;
    documentEditorForm.dataset.documentId = "";
    return;
  }

  documentEditorForm.dataset.documentId = previewDoc.document_id;
  documentEditorTitleInput.value = previewDoc.title || "";
  documentEditorCategoryInput.value = previewDoc.category || "";
  documentEditorFolderInput.value = normalizeFolderPath(previewDoc.folder);
  documentEditorTagsInput.value = stripAutoTagsForFolder(previewDoc.tags || [], previewDoc.folder).join(", ");
  state.library.editorDocumentId = previewDoc.document_id;
  state.library.editorDirty = false;
}

function renderFolderProperties() {
  const folderId = state.library.activeFolderId;
  if (!folderId || getPreviewDocument()) {
    folderPropertiesCard.classList.add("is-hidden");
    return false;
  }

  const watchedFolder = getWatchedFolderForLibraryFolder(folderId);
  const folderDocuments = state.library.documents.filter((documentSummary) =>
    folderPathContainsFolder(folderId, documentSummary.folder)
  );
  const categories = normalizeItems(folderDocuments.map((documentSummary) => documentSummary.category));
  const tags = normalizeTagItems([
    ...(watchedFolder ? watchedFolder.tags || [] : []),
    ...buildFolderAutoTags(folderId),
  ]);

  folderPropertiesTitle.textContent = getFolderDisplayName(folderId, getFolderNameSegment(folderId));
  folderPropertiesKind.textContent = watchedFolder
    ? "Synchronized folder. Rename changes its display alias only."
    : "Library folder. Rename changes its library path.";
  folderPropertiesPath.textContent = formatFolderPath(folderId);
  folderPropertiesDocumentCount.textContent = String(folderDocuments.length);
  folderPropertiesCategory.textContent = watchedFolder
    ? watchedFolder.category || "watched"
    : categories.join(", ") || "No documents";

  folderPropertiesAliasRow.classList.toggle("is-hidden", !watchedFolder);
  folderPropertiesAliasInput.value = watchedFolder ? watchedFolder.alias || "" : "";
  folderPropertiesSourceRow.classList.toggle("is-hidden", !watchedFolder);
  folderPropertiesSource.textContent = watchedFolder ? watchedFolder.source_path : "";

  folderPropertiesScheduleRow.classList.toggle("is-hidden", !watchedFolder);
  folderPropertiesWatchSettings.classList.toggle("is-hidden", !watchedFolder);
  if (watchedFolder) {
    const cadence = watchedFolder.enabled
      ? `Every ${watchedFolder.interval_minutes} minutes`
      : "Paused";
    const lastStatus = watchedFolder.last_status ? `Status: ${watchedFolder.last_status}` : null;
    folderPropertiesSchedule.textContent = [
      cadence,
      `Last sync: ${formatWatchFolderTimestamp(watchedFolder.last_sync_at)}`,
      lastStatus,
    ].filter(Boolean).join(" - ");
    folderPropertiesIntervalInput.value = String(watchedFolder.interval_minutes || 30);
    folderPropertiesCategoryInput.value = watchedFolder.category || "watched";
    folderPropertiesTagsInput.value = normalizeTagItems(watchedFolder.tags || []).join(", ");
    folderPropertiesRecursiveInput.checked = Boolean(watchedFolder.recursive);
    folderPropertiesEnabledInput.checked = Boolean(watchedFolder.enabled);
  } else {
    folderPropertiesSchedule.textContent = "";
    folderPropertiesIntervalInput.value = "30";
    folderPropertiesCategoryInput.value = "watched";
    folderPropertiesTagsInput.value = "";
    folderPropertiesRecursiveInput.checked = true;
    folderPropertiesEnabledInput.checked = true;
  }

  folderPropertiesTags.innerHTML = "";
  if (tags.length === 0) {
    const empty = document.createElement("span");
    empty.className = "folder-property-empty";
    empty.textContent = "No tags";
    folderPropertiesTags.appendChild(empty);
  } else {
    tags.forEach((tag) => {
      const chip = document.createElement("span");
      chip.className = "folder-property-tag";
      chip.textContent = tag;
      folderPropertiesTags.appendChild(chip);
    });
  }

  renameSelectedFolderButton.textContent = watchedFolder ? "Save settings" : "Rename folder";
  renameSelectedFolderButton.disabled = watchedFolder
    ? state.library.watchFolderInFlight
    : !canMutateLibrary();
  folderPropertiesAliasInput.disabled = !watchedFolder || state.library.watchFolderInFlight;
  syncSelectedFolderButton.classList.toggle("is-hidden", !watchedFolder);
  syncSelectedFolderButton.disabled = !watchedFolder || state.library.watchFolderInFlight;
  syncSelectedFolderButton.textContent = state.library.watchFolderInFlight ? "Syncing..." : "Sync now";

  folderPropertiesCard.classList.toggle("is-watched", Boolean(watchedFolder));
  folderPropertiesCard.classList.remove("is-hidden");
  return true;
}

function renderDocumentEditor(options = {}) {
  const { force = false } = options;
  const previewDoc = getPreviewDocument();

  if (!previewDoc) {
    state.library.editorDismissed = false;
    documentEditorForm.classList.add("is-hidden");
    hydrateDocumentEditor(null);
    if (renderFolderProperties()) {
      documentEditorEmpty.classList.add("is-hidden");
      setDocumentEditorState(false);
      return;
    }
    documentEditorEmpty.classList.remove("is-hidden");
    documentEditorEmpty.textContent = "Select a folder to inspect its properties, or select a document to edit its metadata.";
    setDocumentEditorStatus("Select a document to rename it, move it, or update its tags.");
    setDocumentEditorState(false);
    return;
  }

  folderPropertiesCard.classList.add("is-hidden");

  const shouldHydrate = force || state.library.editorDocumentId !== previewDoc.document_id || !state.library.editorDirty;
  if (shouldHydrate) {
    hydrateDocumentEditor(previewDoc);
  }

  if (state.library.editorDismissed) {
    documentEditorEmpty.classList.remove("is-hidden");
    documentEditorForm.classList.add("is-hidden");
    documentEditorEmpty.textContent = "Metadata editor closed. Select this file again to reopen it.";
    setDocumentEditorState(false);
    return;
  }

  documentEditorEmpty.classList.add("is-hidden");
  documentEditorForm.classList.remove("is-hidden");
  documentEditorEmpty.textContent = "Select a document to rename it, move it to another folder, or update its tags.";
  documentEditorId.textContent = hasGeneratedUploadId(previewDoc.document_id)
    ? "Hidden upload ID retained behind the scenes"
    : `Stored ID: ${previewDoc.document_id}`;

  if (canMutateLibrary()) {
    setDocumentEditorStatus("Change the folder path to move this file elsewhere in the explorer.");
  } else {
    setDocumentEditorStatus("Metadata editing is only available for local json and semantic libraries.");
  }
  setDocumentEditorState(state.library.metadataUpdateInFlight);
}

async function saveDocumentChanges() {
  const previewDoc = getPreviewDocument();
  if (!previewDoc || state.library.metadataUpdateInFlight) {
    return;
  }

  if (!canMutateLibrary()) {
    setDocumentEditorStatus("Metadata editing is only available for local json and semantic libraries.", "error");
    return;
  }

  const nextTitle = documentEditorTitleInput.value.trim();
  const nextCategory = documentEditorCategoryInput.value.trim();
  const nextFolder = normalizeFolderInputValue(documentEditorFolderInput.value, nextCategory || previewDoc.category);
  const nextTags = normalizeTagItems(documentEditorTagsInput.value);

  if (!nextTitle) {
    setDocumentEditorStatus("Title is required.", "error");
    return;
  }
  if (!nextCategory) {
    setDocumentEditorStatus("Category is required.", "error");
    return;
  }

  const currentTitle = String(previewDoc.title || "").trim();
  const currentCategory = String(previewDoc.category || "").trim();
  const currentFolder = normalizeFolderPath(previewDoc.folder);
  const currentTags = stripAutoTagsForFolder(previewDoc.tags || [], currentFolder);

  if (
    currentTitle === nextTitle &&
    currentCategory === nextCategory &&
    currentFolder === nextFolder &&
    currentTags.join("|") === nextTags.join("|")
  ) {
    setDocumentEditorStatus("No metadata changes to save.");
    return;
  }

  setDocumentEditorState(true);
  setDocumentEditorStatus("Saving metadata and refreshing the embedded library...");

  try {
    const response = await fetch("/api/documents/metadata", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        document_id: previewDoc.document_id,
        title: nextTitle,
        category: nextCategory,
        folder: nextFolder,
        tags: nextTags,
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Metadata update failed";
      throw new Error(detail);
    }

    state.library.previewCache = {};
    await loadDocumentLibrary();
    await openDocumentPreview(previewDoc.document_id, { revealInExplorer: true });
    renderDocumentEditor({ force: true });

    const message = payload && payload.message
      ? payload.message
      : `Updated metadata for ${previewDoc.document_id}.`;
    setDocumentEditorStatus(message, "success");
    setLibraryActionStatus(message, "success");
  } catch (error) {
    setDocumentEditorStatus(error.message, "error");
    setLibraryActionStatus(error.message, "error");
  } finally {
    setDocumentEditorState(false);
  }
}

async function renameDocumentFromContext(documentId) {
  const docSummary = getDocumentSummary(documentId);
  if (!docSummary) {
    setLibraryActionStatus("The selected file could not be found.", "error");
    return;
  }
  if (!canMutateLibrary()) {
    setLibraryActionStatus("File rename is only available for local json and semantic libraries.", "error");
    return;
  }

  const nextTitleRaw = window.prompt("Rename file", docSummary.title || getDocumentDisplayLabel(docSummary));
  if (nextTitleRaw === null) {
    return;
  }

  const nextTitle = nextTitleRaw.trim();
  if (!nextTitle) {
    setLibraryActionStatus("File title is required.", "error");
    return;
  }
  if (nextTitle === String(docSummary.title || "").trim()) {
    return;
  }

  const previousPreviewId = state.library.previewDocumentId;
  setLibraryActionStatus(`Renaming ${docSummary.title || documentId}...`);

  try {
    const response = await fetch("/api/documents/metadata", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        document_id: docSummary.document_id,
        title: nextTitle,
        category: docSummary.category,
        folder: docSummary.folder,
        tags: docSummary.tags || [],
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "File rename failed";
      throw new Error(detail);
    }

    state.library.previewCache = {};
    state.library.editorDismissed = false;
    await loadDocumentLibrary();
    if (previousPreviewId && getDocumentSummary(previousPreviewId)) {
      await openDocumentPreview(previousPreviewId, { revealInExplorer: false });
    } else {
      renderPreview();
      renderDocumentEditor();
    }
    setLibraryActionStatus(payload.message || `Renamed ${documentId}.`, "success");
  } catch (error) {
    setLibraryActionStatus(error.message, "error");
  }
}

async function renameFolderFromContext(folderId, nextNameValue) {
  if (!folderId) {
    return false;
  }

  if (nextNameValue === undefined) {
    beginFolderInlineRename(folderId);
    return true;
  }

  const watchedFolder = getWatchedFolderForLibraryFolder(folderId);
  if (watchedFolder) {
    return updateWatchedFolderAlias(watchedFolder, nextNameValue);
  }

  if (!canMutateLibrary()) {
    setLibraryActionStatus("Folder rename is only available for local json and semantic libraries.", "error");
    return false;
  }

  const currentName = getFolderNameSegment(folderId);
  const nextName = String(nextNameValue || "").trim();
  if (!nextName) {
    setLibraryActionStatus("Folder name is required.", "error");
    return false;
  }
  if (nextName.includes("/") || nextName.includes("\\")) {
    setLibraryActionStatus("Folder rename accepts a single folder name, not a path.", "error");
    return false;
  }
  if (nextName === currentName) {
    return true;
  }

  const previousPreviewId = state.library.previewDocumentId;
  setLibraryActionStatus(`Renaming folder ${formatFolderPath(folderId)}...`);

  try {
    const response = await fetch("/api/folders/rename", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        folder_id: folderId,
        new_name: nextName,
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Folder rename failed";
      throw new Error(detail);
    }

    syncFolderRenameAcrossState(payload.folder_id, payload.renamed_folder_id);
    state.library.previewCache = {};
    state.library.editorDismissed = false;
    await loadDocumentLibrary();
    if (previousPreviewId && getDocumentSummary(previousPreviewId)) {
      await openDocumentPreview(previousPreviewId, { revealInExplorer: false });
    } else {
      renderPreview();
      renderDocumentEditor();
    }
    setLibraryActionStatus(
      payload.message || `Renamed folder to ${formatFolderPath(payload.renamed_folder_id)}.`,
      "success"
    );
    return true;
  } catch (error) {
    setLibraryActionStatus(error.message, "error");
    return false;
  }
}

async function deleteDocumentFromContext(documentId) {
  const docSummary = getDocumentSummary(documentId);
  if (!docSummary) {
    setLibraryActionStatus("The selected file could not be found.", "error");
    return;
  }
  if (!canMutateLibrary()) {
    setLibraryActionStatus("Deletion is only available for local json and semantic libraries.", "error");
    return;
  }
  if (state.library.deleteInFlight) {
    return;
  }

  const confirmMessage = `Delete ${docSummary.title || getDocumentDisplayLabel(docSummary)} from the local library?`;
  if (!window.confirm(confirmMessage)) {
    return;
  }

  setDeleteState(true);
  setLibraryActionStatus(`Deleting ${docSummary.title || documentId}...`);

  try {
    const response = await fetch("/api/documents/delete", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        document_ids: [documentId],
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Delete failed";
      throw new Error(detail);
    }

    state.library.previewCache = {};
    if (state.library.previewDocumentId === documentId) {
      state.library.previewDocumentId = null;
    }
    state.library.deleteSelectionIds = state.library.deleteSelectionIds.filter((item) => item !== documentId);
    await loadDocumentLibrary();
    renderDeleteSelectionSummary();
    renderPreview();
    renderDocumentEditor();
    setLibraryActionStatus(payload.message || `Deleted ${documentId}.`, "success");
  } catch (error) {
    setLibraryActionStatus(error.message, "error");
  } finally {
    setDeleteState(false);
  }
}

async function deleteFolderFromContext(folderId) {
  if (!folderId) {
    return;
  }
  if (!canMutateLibrary()) {
    setLibraryActionStatus("Folder deletion is only available for local json and semantic libraries.", "error");
    return;
  }
  if (state.library.deleteInFlight) {
    return;
  }

  const folderLabel = formatFolderPath(folderId);
  const scopedDocumentIds = getFolderDocumentIds(folderId);
  const synchronizedFolders = getWatchedFoldersWithinLibraryFolder(folderId);
  const docCount = scopedDocumentIds.length;
  let confirmMessage = docCount
    ? `Delete folder ${folderLabel} and ${docCount} document${docCount === 1 ? "" : "s"} inside it?`
    : `Delete empty folder ${folderLabel}?`;
  if (synchronizedFolders.length > 0) {
    confirmMessage += (
      ` This will also unsynchronize ${synchronizedFolders.length} source folder` +
      `${synchronizedFolders.length === 1 ? "" : "s"}; source files on disk will not be deleted.`
    );
  }
  if (!window.confirm(confirmMessage)) {
    return;
  }

  setDeleteState(true);
  setLibraryActionStatus(`Deleting folder ${folderLabel}...`);

  try {
    const response = await fetch("/api/folders/delete", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        folder_id: folderId,
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Folder delete failed";
      throw new Error(detail);
    }

    state.library.previewCache = {};
    if (state.library.previewDocumentId && scopedDocumentIds.includes(state.library.previewDocumentId)) {
      state.library.previewDocumentId = null;
    }
    state.library.deleteSelectionIds = state.library.deleteSelectionIds.filter(
      (item) => !scopedDocumentIds.includes(item)
    );
    await loadWatchedFolders();
    await loadDocumentLibrary();
    renderDeleteSelectionSummary();
    renderPreview();
    renderDocumentEditor();
    setLibraryActionStatus(payload.message || `Deleted folder ${folderLabel}.`, "success");
  } catch (error) {
    setLibraryActionStatus(error.message, "error");
  } finally {
    setDeleteState(false);
  }
}

async function moveDocumentToFolder(documentId, targetFolderId) {
  const docSummary = getDocumentSummary(documentId);
  if (!docSummary) {
    setLibraryActionStatus("The dragged file could not be found.", "error");
    return;
  }
  if (!targetFolderId) {
    setLibraryActionStatus("Files can only be dropped onto a folder.", "error");
    return;
  }
  if (!canMutateLibrary()) {
    setLibraryActionStatus("File moves are only available for local json and semantic libraries.", "error");
    return;
  }

  const normalizedTargetFolderId = normalizeFolderPath(targetFolderId);
  if (normalizeFolderPath(docSummary.folder) === normalizedTargetFolderId) {
    return;
  }

  const previousPreviewId = state.library.previewDocumentId;
  setLibraryActionStatus(`Moving ${docSummary.title || documentId} to ${formatFolderPath(normalizedTargetFolderId)}...`);

  try {
    const response = await fetch("/api/documents/metadata", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        document_id: docSummary.document_id,
        title: docSummary.title,
        category: docSummary.category,
        folder: normalizedTargetFolderId,
        tags: docSummary.tags || [],
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "File move failed";
      throw new Error(detail);
    }

    state.library.previewCache = {};
    state.library.editorDismissed = false;
    await loadDocumentLibrary();
    expandFolderAncestors(normalizedTargetFolderId);
    state.library.activeFolderId = normalizedTargetFolderId;
    if (previousPreviewId && getDocumentSummary(previousPreviewId)) {
      await openDocumentPreview(previousPreviewId, { revealInExplorer: false });
    } else {
      renderPreview();
      renderDocumentEditor();
    }
    setLibraryActionStatus(payload.message || `Moved ${documentId}.`, "success");
  } catch (error) {
    setLibraryActionStatus(error.message, "error");
  }
}

async function moveFolderToFolder(folderId, targetFolderId) {
  if (!folderId) {
    return;
  }
  if (!canMutateLibrary()) {
    setLibraryActionStatus("Folder moves are only available for local json and semantic libraries.", "error");
    return;
  }

  const normalizedTargetFolderId = targetFolderId ? normalizeFolderPath(targetFolderId) : null;
  if (!isValidFolderDropTarget({ type: "folder", folderId }, normalizedTargetFolderId)) {
    return;
  }

  setLibraryActionStatus(
    normalizedTargetFolderId
      ? `Moving folder to ${formatFolderPath(normalizedTargetFolderId)}...`
      : `Moving folder ${formatFolderPath(folderId)} to the top level...`
  );

  try {
    const previousPreviewId = state.library.previewDocumentId;
    const response = await fetch("/api/folders/move", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        folder_id: folderId,
        new_parent_folder_id: normalizedTargetFolderId,
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Folder move failed";
      throw new Error(detail);
    }

    syncFolderRenameAcrossState(payload.folder_id, payload.moved_folder_id);
    state.library.previewCache = {};
    state.library.editorDismissed = false;
    await loadDocumentLibrary();
    expandFolderAncestors(payload.moved_folder_id);
    state.library.activeFolderId = payload.moved_folder_id;
    if (previousPreviewId && getDocumentSummary(previousPreviewId)) {
      await openDocumentPreview(previousPreviewId, { revealInExplorer: false });
    } else {
      renderPreview();
      renderDocumentEditor();
    }
    setLibraryActionStatus(payload.message || `Moved folder to ${formatFolderPath(payload.moved_folder_id)}.`, "success");
  } catch (error) {
    setLibraryActionStatus(error.message, "error");
  }
}

async function createFolderFromContext(parentFolderId) {
  if (!canMutateLibrary()) {
    setLibraryActionStatus("Folder creation is only available for local json and semantic libraries.", "error");
    return;
  }

  const promptTitle = parentFolderId
    ? `New folder in ${formatFolderPath(parentFolderId)}`
    : "New top-level folder";
  const nextNameRaw = window.prompt(promptTitle, "");
  if (nextNameRaw === null) {
    return;
  }

  const nextName = nextNameRaw.trim();
  if (!nextName) {
    setLibraryActionStatus("Folder name is required.", "error");
    return;
  }
  if (nextName.includes("/") || nextName.includes("\\")) {
    setLibraryActionStatus("Folder creation accepts a single folder name, not a path.", "error");
    return;
  }

  setLibraryActionStatus(
    parentFolderId
      ? `Creating folder in ${formatFolderPath(parentFolderId)}...`
      : `Creating folder ${nextName}...`
  );

  try {
    const response = await fetch("/api/folders/create", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        folder_name: nextName,
        parent_folder_id: parentFolderId || null,
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Folder creation failed";
      throw new Error(detail);
    }

    await loadDocumentLibrary();
    expandFolderAncestors(payload.folder_id);
    state.library.activeFolderId = payload.folder_id;
    state.library.searchQuery = "";
    renderBrowserStats();
    renderLibraryExplorer();
    renderPreview();
    renderDocumentEditor();
    setLibraryActionStatus(payload.message || `Created folder ${formatFolderPath(payload.folder_id)}.`, "success");
  } catch (error) {
    setLibraryActionStatus(error.message, "error");
  }
}

function renderPreview() {
  const previewDoc = getPreviewDocument();
  if (!previewDoc) {
    previewEmpty.textContent = "Select a document to preview its indexed content and metadata.";
    previewEmpty.classList.remove("is-hidden");
    previewCard.classList.add("is-hidden");
    return;
  }

  previewBadges.innerHTML = "";
  previewCard.dataset.documentId = previewDoc.document_id;
  [
    `Folder: ${formatFolderDisplayPath(previewDoc.folder)}`,
    `Category: ${previewDoc.category}`,
    previewDoc.embedded ? "Embedded" : "Not embedded",
    previewDoc.chunk_count === null || previewDoc.chunk_count === undefined
      ? null
      : `${previewDoc.chunk_count} chunk${previewDoc.chunk_count === 1 ? "" : "s"}`,
  ]
    .filter(Boolean)
    .forEach((label) => {
      const badge = document.createElement("span");
      badge.className = "context-chip";
      badge.textContent = label;
      previewBadges.appendChild(badge);
    });

  previewTitle.textContent = getDocumentDisplayLabel(previewDoc);
  previewSummary.innerHTML = renderMarkdown(previewDoc.summary || "No summary available.");
  previewMeta.textContent = [
    previewDoc.updated_at ? `Updated ${previewDoc.updated_at}` : null,
    previewDoc.source_url ? previewDoc.source_url : null,
    previewDoc.tags && previewDoc.tags.length > 0 ? `Tags: ${previewDoc.tags.join(", ")}` : null,
  ]
    .filter(Boolean)
    .join(" - ");
  previewText.innerHTML = renderMarkdown(previewDoc.text);

  previewEmpty.classList.add("is-hidden");
  previewCard.classList.remove("is-hidden");
}

function openDocumentBrowser() {
  if (!state.library.loaded && !state.library.loadError) {
    state.library.collapseFoldersOnLoad = true;
    void loadDocumentLibrary();
  }
  if (!state.library.watchFoldersLoaded && !state.library.watchFoldersLoadError) {
    void loadWatchedFolders();
  }
  state.library.editorDismissed = false;
  closeExplorerContextMenu();
  state.library.collapsedFolderIds = getAllLibraryFolderPathIds();
  state.library.draftContext = cloneContextFilter(state.library.appliedContext);
  renderBrowserStats();
  renderDeleteSelectionSummary();
  renderLibraryExplorer();
  renderPreview();
  documentBrowser.classList.remove("is-hidden");
  documentBrowser.setAttribute("aria-hidden", "false");
}

function closeDocumentBrowser() {
  closeExplorerContextMenu();
  closeSynchronizedPathsMenu();
  documentBrowser.classList.add("is-hidden");
  documentBrowser.setAttribute("aria-hidden", "true");
}

function applyContextSelection(nextContext) {
  state.library.appliedContext = {
    folderIds: normalizeItems(nextContext.folderIds),
    documentIds: normalizeItems(nextContext.documentIds),
  };
  state.library.draftContext = cloneContextFilter(state.library.appliedContext);
  renderContextSummary();
  renderBrowserStats();
  renderScopePane();
}

async function generateDocumentFromContext(event) {
  if (event) {
    event.preventDefault();
  }
  if (!documentGenerationForm || state.generation.inFlight) {
    return;
  }

  const instructions = String(documentGenerationInstructionsInput.value || "").trim();
  const title = String(documentGenerationTitleInput.value || "").trim();
  const outputFormat = String(documentGenerationFormatSelect.value || "docx");

  if (!instructions) {
    setDocumentGenerationStatus("Document instructions are required.", "error");
    documentGenerationInstructionsInput.focus();
    return;
  }

  setDocumentGenerationState(true);
  setDocumentGenerationStatus("Generating a file from the current library scope...");

  try {
    const response = await fetch("/api/documents/generate", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        title: title || null,
        instructions,
        output_format: outputFormat,
        source_mode: state.sourceMode,
        context_filter: {
          folder_ids: state.library.appliedContext.folderIds,
          document_ids: state.library.appliedContext.documentIds,
        },
      }),
    });

    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(getErrorMessageFromPayload(payload));
    }
    if (!payload || !payload.content_base64) {
      throw new Error("The server returned an empty generated file.");
    }

    downloadBase64File(payload.filename, payload.mime_type, payload.content_base64);
    setDocumentGenerationStatus(
      payload.message || `Generated ${payload.filename || "document"} and started the download.`,
      "success"
    );
  } catch (error) {
    setDocumentGenerationStatus(error.message, "error");
  } finally {
    setDocumentGenerationState(false);
  }
}

async function sendMessage(message) {
  if (!message || state.sending) {
    return;
  }

  renderMessage({
    role: "user",
    label: "You",
    body: message,
  });

  setComposerState(true);
  showResponsePreparationIndicator();
  setConversationMemoryStatus("Preparing response. This chat will autosave after the reply.");

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        conversation_id: state.conversationId,
        message,
        source_mode: state.sourceMode,
        context_filter: {
          folder_ids: state.library.appliedContext.folderIds,
          document_ids: state.library.appliedContext.documentIds,
        },
      }),
    });

    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Unknown server error";
      throw new Error(detail);
    }

    hideResponsePreparationIndicator();
    state.conversationId = payload.conversation_id;
    renderMessage({
      role: "assistant",
      label: "Assistant",
      body: payload.assistant_message,
      citations: payload.citations || [],
      toolTrace: payload.tool_trace || [],
      generatedDocument: payload.generated_document || null,
    });
    await saveCurrentConversation({ silent: true });
  } catch (error) {
    hideResponsePreparationIndicator();
    renderMessage({
      role: "system",
      label: "System",
      body: `Request failed: ${error.message}`,
    });
    updateConversationMemoryStatus();
  } finally {
    hideResponsePreparationIndicator();
    setComposerState(false);
    messageInput.focus();
  }
}

composerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = messageInput.value.trim();
  messageInput.value = "";
  await sendMessage(message);
});

messageInput.addEventListener("keydown", (event) => {
  if (
    event.key !== "Enter" ||
    event.shiftKey ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.isComposing
  ) {
    return;
  }

  event.preventDefault();
  if (!state.sending) {
    composerForm.requestSubmit();
  }
});

if (documentGenerationForm) {
  documentGenerationForm.addEventListener("submit", async (event) => {
    await generateDocumentFromContext(event);
  });
}

uploadForm.addEventListener("submit", async (event) => {
  await uploadDocument(event);
});

selectUploadFilesButton.addEventListener("click", () => {
  uploadFileInput.click();
});

uploadFileInput.addEventListener("change", () => {
  if (uploadFileInput.files && uploadFileInput.files.length > 0) {
    uploadDirectoryInput.value = "";
    uploadForm.classList.remove("is-hidden");
    const count = uploadFileInput.files.length;
    setUploadStatus(
      `${count} file${count === 1 ? "" : "s"} selected from your local filesystem.`,
      "neutral"
    );
  }
});

selectUploadDirectoryButton.addEventListener("click", () => {
  uploadDirectoryInput.click();
});

uploadDirectoryInput.addEventListener("change", () => {
  if (uploadDirectoryInput.files && uploadDirectoryInput.files.length > 0) {
    uploadFileInput.value = "";
    uploadForm.classList.remove("is-hidden");
    const count = uploadDirectoryInput.files.length;
    const rootPath = String(uploadDirectoryInput.files[0].webkitRelativePath || "");
    const rootFolder = rootPath.split("/").filter(Boolean)[0] || "selected folder";
    setUploadStatus(
      `${count} file${count === 1 ? "" : "s"} queued from ${rootFolder}. Relative folders will be preserved.`,
      "neutral"
    );
  }
});

if (syncFolderActionButton) {
  syncFolderActionButton.addEventListener("click", async () => {
    await selectAndCreateWatchedFolder();
  });
}

if (watchFolderForm) {
  watchFolderForm.addEventListener("submit", addWatchedFolder);
}

if (browseWatchRootPathButton) {
  browseWatchRootPathButton.addEventListener("click", browseForWatchRootPath);
}

if (syncAllWatchFoldersButton) {
  syncAllWatchFoldersButton.addEventListener("click", syncAllWatchedFolders);
}

if (openSynchronizedPathsButton) {
  openSynchronizedPathsButton.addEventListener("click", openSynchronizedPathsMenu);
}

if (closeSynchronizedPathsButton) {
  closeSynchronizedPathsButton.addEventListener("click", closeSynchronizedPathsMenu);
}

document.querySelectorAll("[data-close-synchronized-paths]").forEach((element) => {
  element.addEventListener("click", closeSynchronizedPathsMenu);
});

if (addSynchronizedPathButton) {
  addSynchronizedPathButton.addEventListener("click", async () => {
    await selectAndCreateWatchedFolder();
  });
}

if (librarySyncAllButton) {
  librarySyncAllButton.addEventListener("click", syncAllWatchedFolders);
}

newConversationButton.addEventListener("click", () => {
  closeSavedConversationContextMenu();
  resetConversation();
  messageInput.focus();
});

if (saveConversationButton) {
  saveConversationButton.addEventListener("click", async () => {
    await saveCurrentConversation();
  });
}

if (conversationSearchInput) {
  conversationSearchInput.addEventListener("input", () => {
    state.memory.searchQuery = conversationSearchInput.value;
    renderSavedConversationList();
  });
}

if (savedConversationRenameButton) {
  savedConversationRenameButton.addEventListener("click", async () => {
    const conversationId = state.memory.contextMenu.conversationId;
    closeSavedConversationContextMenu();
    if (conversationId) {
      await renameSavedConversation(conversationId);
    }
  });
}

if (savedConversationDeleteButton) {
  savedConversationDeleteButton.addEventListener("click", async () => {
    const conversationId = state.memory.contextMenu.conversationId;
    closeSavedConversationContextMenu();
    if (conversationId) {
      await deleteSavedConversation(conversationId);
    }
  });
}

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", async () => {
    const prompt = button.getAttribute("data-prompt");
    if (!prompt) {
      return;
    }
    messageInput.value = prompt;
    await sendMessage(prompt);
    messageInput.value = "";
  });
});

modeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const sourceMode = button.dataset.sourceMode;
    if (!sourceMode || sourceMode === state.sourceMode) {
      return;
    }
    applySourceMode(sourceMode);
    persistActiveConversationPreferences();
    const sourceModeLabel = sourceMode === "broader" ? "Broader view" : "Internal docs";
    setConversationMemoryStatus(
      `${sourceModeLabel} is now active and remembered for this conversation.`,
      "success"
    );
  });
});

openLibraryButton.addEventListener("click", () => {
  closeSavedConversationContextMenu();
  openDocumentBrowser();
});

closeBrowserButton.addEventListener("click", () => {
  closeDocumentBrowser();
});

document.querySelectorAll("[data-close-browser]").forEach((element) => {
  element.addEventListener("click", () => {
    closeDocumentBrowser();
  });
});

applyContextButton.addEventListener("click", () => {
  const nextContext = cloneContextFilter(state.library.draftContext);
  const changed = !contextFiltersEqual(nextContext, state.library.appliedContext);
  if (changed) {
    applyContextSelection(nextContext);
    persistActiveConversationPreferences();
    setLibraryActionStatus("Scope applied and remembered for the current conversation.", "success");
  } else {
    setLibraryActionStatus("Draft scope already matches the applied scope.");
  }
  renderLibraryExplorer();
});

browserUseAllButton.addEventListener("click", () => {
  state.library.draftContext = {
    folderIds: [],
    documentIds: [],
  };
  renderBrowserStats();
  setLibraryActionStatus("Draft scope reset. All indexed documents are available.", "success");
  renderLibraryExplorer();
});

explorerRootButton.addEventListener("click", () => {
  setActiveFolder(null);
});

explorerExpandAllButton.addEventListener("click", () => {
  setAllFoldersCollapsed(false);
});

explorerCollapseAllButton.addEventListener("click", () => {
  setAllFoldersCollapsed(true);
});

explorerRevealSelectionButton.addEventListener("click", () => {
  const previewDoc = getPreviewDocument();
  if (!previewDoc) {
    return;
  }
  state.library.activeFolderId = normalizeFolderPath(previewDoc.folder);
  state.library.searchQuery = "";
  renderBrowserStats();
  renderLibraryExplorer();
});

librarySearchInput.addEventListener("input", () => {
  state.library.searchQuery = librarySearchInput.value;
  renderBrowserStats();
  renderFolderTree();
  renderDocumentFileList();
});

folderTreeSurface.addEventListener("contextmenu", (event) => {
  if (!canMutateLibrary()) {
    return;
  }
  const rowElement = event.target instanceof Element ? event.target.closest(".folder-tree-row") : null;
  if (rowElement) {
    return;
  }
  event.preventDefault();
  openExplorerContextMenu({
    targetType: "folder-space",
    targetId: state.library.activeFolderId,
    label: state.library.activeFolderId ? formatFolderDisplayPath(state.library.activeFolderId) : "All documents",
    x: event.clientX,
    y: event.clientY,
  });
});

deleteSelectedButton.addEventListener("click", async () => {
  await deleteSelectedDocuments();
});

documentEditorForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await saveDocumentChanges();
});

explorerContextNewFolderButton.addEventListener("click", async () => {
  const contextTarget = { ...state.library.contextMenu };
  closeExplorerContextMenu();
  if (contextTarget.targetType === "folder") {
    await createFolderFromContext(contextTarget.targetId);
    return;
  }
  if (contextTarget.targetType === "folder-space") {
    await createFolderFromContext(contextTarget.targetId);
  }
});

explorerContextRenameButton.addEventListener("click", async () => {
  const contextTarget = { ...state.library.contextMenu };
  closeExplorerContextMenu();
  if (contextTarget.targetType === "folder") {
    await renameFolderFromContext(contextTarget.targetId);
    return;
  }
  if (contextTarget.targetType === "document") {
    await renameDocumentFromContext(contextTarget.targetId);
  }
});

explorerContextDeleteButton.addEventListener("click", async () => {
  const contextTarget = { ...state.library.contextMenu };
  closeExplorerContextMenu();
  if (contextTarget.targetType === "folder") {
    await deleteFolderFromContext(contextTarget.targetId);
    return;
  }
  if (contextTarget.targetType === "document") {
    await deleteDocumentFromContext(contextTarget.targetId);
  }
});

closeDocumentEditorButton.addEventListener("click", () => {
  closeDocumentEditor();
});

renameSelectedFolderButton.addEventListener("click", async () => {
  if (!state.library.activeFolderId) {
    return;
  }
  const watchedFolder = getWatchedFolderForLibraryFolder(state.library.activeFolderId);
  if (watchedFolder) {
    await updateWatchedFolderSettings(watchedFolder);
    return;
  }
  await renameFolderFromContext(state.library.activeFolderId);
});

folderPropertiesAliasInput.addEventListener("keydown", async (event) => {
  if (event.key !== "Enter") {
    return;
  }
  event.preventDefault();
  const watchedFolder = getWatchedFolderForLibraryFolder(state.library.activeFolderId);
  if (watchedFolder) {
    await updateWatchedFolderSettings(watchedFolder);
  }
});

syncSelectedFolderButton.addEventListener("click", async () => {
  const watchedFolder = getWatchedFolderForLibraryFolder(state.library.activeFolderId);
  if (watchedFolder) {
    await syncWatchedFolder(watchedFolder.watch_id);
  }
});

closeFolderPropertiesButton.addEventListener("click", () => {
  setActiveFolder(null);
});

[documentEditorTitleInput, documentEditorCategoryInput, documentEditorFolderInput, documentEditorTagsInput].forEach((element) => {
  element.addEventListener("input", () => {
    state.library.editorDirty = true;
  });
});

clearContextButton.addEventListener("click", () => {
  if (
    state.library.appliedContext.folderIds.length === 0 &&
    state.library.appliedContext.documentIds.length === 0
  ) {
    return;
  }
  applyContextSelection({
    folderIds: [],
    documentIds: [],
  });
  persistActiveConversationPreferences();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.memory.contextMenu.open) {
    event.preventDefault();
    closeSavedConversationContextMenu();
    return;
  }

  if (event.key === "Escape" && !documentBrowser.classList.contains("is-hidden")) {
    if (synchronizedPathsMenu && !synchronizedPathsMenu.classList.contains("is-hidden")) {
      event.preventDefault();
      closeSynchronizedPathsMenu();
      return;
    }
    if (state.library.contextMenu.open) {
      event.preventDefault();
      closeExplorerContextMenu();
      return;
    }
    if (!documentEditorForm.classList.contains("is-hidden")) {
      event.preventDefault();
      closeDocumentEditor();
      return;
    }
    if (!folderPropertiesCard.classList.contains("is-hidden")) {
      event.preventDefault();
      setActiveFolder(null);
      return;
    }
    closeDocumentBrowser();
  }
});

document.addEventListener("pointerdown", (event) => {
  if (
    state.memory.contextMenu.open &&
    savedConversationContextMenu &&
    !savedConversationContextMenu.contains(event.target) &&
    !savedConversationList.contains(event.target)
  ) {
    closeSavedConversationContextMenu();
  }

  if (!state.library.contextMenu.open) {
    return;
  }
  if (explorerContextMenu.contains(event.target)) {
    return;
  }
  closeExplorerContextMenu();
});

window.addEventListener("resize", () => {
  if (state.memory.contextMenu.open) {
    closeSavedConversationContextMenu();
  }
  if (state.library.contextMenu.open) {
    closeExplorerContextMenu();
  }
});

documentBrowser.addEventListener("scroll", () => {
  if (state.library.contextMenu.open) {
    closeExplorerContextMenu();
  }
}, true);

applySourceMode(state.sourceMode);
renderContextSummary();
renderDeleteSelectionSummary();
renderScopePane();
setLibraryActionStatus("Browse the library, adjust scope, or select a file to edit its metadata.");
setDocumentEditorStatus("Select a document to rename it, move it, or update its tags.");
setDocumentEditorState(false);
setDocumentGenerationState(false);
setDocumentGenerationStatus("Uses the active source mode and currently applied library scope.");
setUploadStatus("Choose files or a local folder, including Dropbox-synced folders, to import and embed in one pass.");
renderSavedConversationList();
renderConversationSaveButton();
resetConversation();
void loadHealth();
void loadSavedConversations();
void loadDocumentLibrary();
