/*
 * Ask Jenny browser application.
 *
 * This is a dependency-free, single-page UI. The central `state` object is the
 * client-side source of truth; event handlers update it, render functions project
 * it into the DOM, and async functions synchronize it with FastAPI. Keeping those
 * three roles distinct is the key to following this otherwise large file.
 */

// Human-readable policy text for the two server-side source modes.
const modeCopy = {
  internal: {
    description: "Prioritizes internal documents and avoids filling gaps with general knowledge.",
    composerNote: "Internal facts should come from the datastore, not memory.",
    intro:
      "Ask about your internal documents or any general question. In this mode, company-specific answers should stay grounded in the datastore first.",
  },
  broader: {
    description:
      "Uses global knowledge and public web context without an active internal document scope.",
    composerNote:
      "Global context is active. Choose Context: Internal to use library documents.",
    intro:
      "Ask for general knowledge, current public information, or broader explainers. Choose Context: Internal when you want an answer grounded in library documents.",
  },
};

const CHAT_PREFERENCES_STORAGE_KEY = "business-knowledge-chat-preferences-v1";
const MAX_STORED_CHAT_PREFERENCES = 100;
const CHAT_SCROLL_STORAGE_KEY = "business-knowledge-chat-scroll-positions-v1";
const MAX_STORED_CHAT_SCROLL_POSITIONS = 100;
const FOLDER_DOUBLE_CLICK_WINDOW_MS = 650;
const PREVIEW_ZOOM_MIN = 0.5;
const PREVIEW_ZOOM_MAX = 2.5;
const PREVIEW_ZOOM_STEP = 0.1;
const UI_SESSION_HEARTBEAT_INTERVAL_MS = 15000;
const CHAT_CLIENT_TIMEOUT_MS = 125000;
const CHAT_IMAGE_MAX_COUNT = 5;
const CHAT_IMAGE_MAX_BYTES = 8 * 1024 * 1024;
const CHAT_IMAGE_MIME_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/gif",
]);
const uiSessionId = crypto.randomUUID();
const conversationPreferenceSyncTimers = new Map();
let conversationScrollSaveTimer = null;
let imageLightboxReturnFocus = null;
let uiSessionHeartbeatTimer = null;
let userManagementReturnFocus = null;
let routinesReturnFocus = null;
let lastFolderControlClick = {
  folderId: null,
  control: null,
  clickedAt: 0,
  x: 0,
  y: 0,
};

const state = {
  conversationId: crypto.randomUUID(),
  sourceMode: "broader",
  reasoningMode: "standard",
  sending: false,
  activeChatRequestId: null,
  activeChatAbortController: null,
  cancelledChatRequestId: null,
  chatImages: [],
  responseIndicatorNode: null,
  responseIndicatorTimer: null,
  messages: [],
  memory: {
    conversations: [],
    loaded: false,
    loadError: null,
    searchQuery: "",
    saveInFlight: false,
    renameInFlightId: null,
    deleteInFlightId: null,
    pairDeleteInFlightIndex: null,
    messageActionMenu: {
      open: false,
      messageIndex: null,
      node: null,
      button: null,
    },
    restoringScrollPosition: false,
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
  auth: {
    user: null,
    loaded: false,
    inFlight: false,
    view: "signin",
  },
  routines: {
    items: [],
    runs: [],
    policy: {},
    systemPaused: false,
    loaded: false,
    inFlight: false,
    draftContext: {
      folderIds: [],
      documentIds: [],
    },
    draftSourceMode: "internal",
    scopePickerOpen: false,
    scopeSearchQuery: "",
    scopeCollapsedFolderIds: [],
    scopeTreeInitialized: false,
    editingId: null,
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
    previewPanelState: "open",
    previewZoom: 1,
    previewCache: {},
    uploadInFlight: false,
    metadataUpdateInFlight: false,
    deleteInFlight: false,
    deleteProgress: null,
    deleteSelectionIds: [],
    editorDocumentId: null,
    editorDirty: false,
    editorDismissed: false,
    watchFolders: [],
    watchFoldersLoaded: false,
    watchFoldersLoadError: null,
    watchFolderInFlight: false,
    openSourceLocationInFlight: false,
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
    scopeSelectionTarget: "chat",
  },
};

// Browser-session heartbeat -------------------------------------------------
// The desktop launcher uses these pings to know whether any UI tabs remain open.
async function heartbeatUiSession() {
  try {
    await fetch(`/api/ui-sessions/${encodeURIComponent(uiSessionId)}`, {
      method: "POST",
      cache: "no-store",
      keepalive: true,
    });
  } catch {
    // The next heartbeat will retry after transient server or network failures.
  }
}

function startUiSessionHeartbeat() {
  if (uiSessionHeartbeatTimer !== null) {
    return;
  }
  void heartbeatUiSession();
  uiSessionHeartbeatTimer = window.setInterval(
    heartbeatUiSession,
    UI_SESSION_HEARTBEAT_INTERVAL_MS
  );
}

function stopUiSessionHeartbeat() {
  if (uiSessionHeartbeatTimer !== null) {
    window.clearInterval(uiSessionHeartbeatTimer);
    uiSessionHeartbeatTimer = null;
  }
  void fetch(`/api/ui-sessions/${encodeURIComponent(uiSessionId)}`, {
    method: "DELETE",
    cache: "no-store",
    keepalive: true,
  }).catch(() => {
    // Expired sessions are pruned server-side if the browser cannot send this request.
  });
}

const messageList = document.getElementById("messageList");
const composerForm = document.getElementById("composerForm");
const messageInput = document.getElementById("messageInput");
const sendButton = document.getElementById("sendButton");
const cancelResponseButton = document.getElementById("cancelResponseButton");
const chatImageInput = document.getElementById("chatImageInput");
const addChatImagesButton = document.getElementById("addChatImagesButton");
const chatImagePreviewList = document.getElementById("chatImagePreviewList");
const sourceModeButton = document.getElementById("sourceModeButton");
const sourceModeLabel = document.getElementById("sourceModeLabel");
const reasoningModeButton = document.getElementById("reasoningModeButton");
const reasoningModeLabel = document.getElementById("reasoningModeLabel");
const newConversationButton = document.getElementById("newConversationButton");
const saveConversationButton = document.getElementById("saveConversationButton");
const conversationSearchInput = document.getElementById("conversationSearchInput");
const savedConversationList = document.getElementById("savedConversationList");
const conversationMemoryStatus = document.getElementById("conversationMemoryStatus");
const savedConversationContextMenu = document.getElementById("savedConversationContextMenu");
const savedConversationRenameButton = document.getElementById("savedConversationRenameButton");
const savedConversationDeleteButton = document.getElementById("savedConversationDeleteButton");
const messageTemplate = document.getElementById("messageTemplate");
const composerNote = document.getElementById("composerNote");
const contextSummary = document.getElementById("contextSummary");
const contextChipList = document.getElementById("contextChipList");
const openLibraryButton = document.getElementById("openLibraryButton");
const openRoutinesButton = document.getElementById("openRoutinesButton");
const routinesModal = document.getElementById("routinesModal");
const routinesBackdrop = document.getElementById("routinesBackdrop");
const closeRoutinesButton = document.getElementById("closeRoutinesButton");
const routineForm = document.getElementById("routineForm");
const routineNameInput = document.getElementById("routineNameInput");
const routineInstructionsInput = document.getElementById("routineInstructionsInput");
const routineOutputSelect = document.getElementById("routineOutputSelect");
const routineScheduleSelect = document.getElementById("routineScheduleSelect");
const routineWeekdayField = document.getElementById("routineWeekdayField");
const routineWeekdaySelect = document.getElementById("routineWeekdaySelect");
const routineTimeInput = document.getElementById("routineTimeInput");
const routineScopeSummary = document.getElementById("routineScopeSummary");
const routineSourceModeButton = document.getElementById("routineSourceModeButton");
const routineSourceModeLabel = document.getElementById("routineSourceModeLabel");
const routineScopeSelectionList = document.getElementById("routineScopeSelectionList");
const routineScopePickerButton = document.getElementById("routineScopePickerButton");
const routineScopePicker = document.getElementById("routineScopePicker");
const routineScopeSearchInput = document.getElementById("routineScopeSearchInput");
const routineScopePickerList = document.getElementById("routineScopePickerList");
const clearRoutineScopeButton = document.getElementById("clearRoutineScopeButton");
const closeRoutineScopePickerButton = document.getElementById("closeRoutineScopePickerButton");
const createRoutineButton = document.getElementById("createRoutineButton");
const routineEditorEyebrow = document.getElementById("routineEditorEyebrow");
const routineEditorHeading = document.getElementById("routineEditorHeading");
const resetRoutineEditorButton = document.getElementById("resetRoutineEditorButton");
const routineStatus = document.getElementById("routineStatus");
const routinePolicySummary = document.getElementById("routinePolicySummary");
const routineList = document.getElementById("routineList");
const routineRunList = document.getElementById("routineRunList");
const routineSystemPauseButton = document.getElementById("routineSystemPauseButton");
const userLoginBadge = document.getElementById("userLoginBadge");
const userLoginLabel = document.getElementById("userLoginLabel");
const userManagementModal = document.getElementById("userManagementModal");
const userManagementBackdrop = document.getElementById("userManagementBackdrop");
const closeUserManagementButton = document.getElementById("closeUserManagementButton");
const userManagementTitle = document.getElementById("userManagementTitle");
const authEyebrow = document.getElementById("authEyebrow");
const authDescription = document.getElementById("authDescription");
const authSignedOutView = document.getElementById("authSignedOutView");
const authAccountView = document.getElementById("authAccountView");
const showSignInButton = document.getElementById("showSignInButton");
const showSignUpButton = document.getElementById("showSignUpButton");
const signInForm = document.getElementById("signInForm");
const signInUsernameInput = document.getElementById("signInUsernameInput");
const signInPasswordInput = document.getElementById("signInPasswordInput");
const signInSubmitButton = document.getElementById("signInSubmitButton");
const signUpForm = document.getElementById("signUpForm");
const signUpDisplayNameInput = document.getElementById("signUpDisplayNameInput");
const signUpUsernameInput = document.getElementById("signUpUsernameInput");
const signUpPasswordInput = document.getElementById("signUpPasswordInput");
const signUpPasswordConfirmInput = document.getElementById("signUpPasswordConfirmInput");
const signUpSubmitButton = document.getElementById("signUpSubmitButton");
const accountAvatar = document.getElementById("accountAvatar");
const accountDisplayName = document.getElementById("accountDisplayName");
const accountUsername = document.getElementById("accountUsername");
const accountRole = document.getElementById("accountRole");
const showChangePasswordButton = document.getElementById("showChangePasswordButton");
const signOutButton = document.getElementById("signOutButton");
const changePasswordForm = document.getElementById("changePasswordForm");
const changePasswordTitle = document.getElementById("changePasswordTitle");
const changePasswordGuidance = document.getElementById("changePasswordGuidance");
const currentPasswordInput = document.getElementById("currentPasswordInput");
const newPasswordInput = document.getElementById("newPasswordInput");
const newPasswordConfirmInput = document.getElementById("newPasswordConfirmInput");
const changePasswordSubmitButton = document.getElementById("changePasswordSubmitButton");
const cancelChangePasswordButton = document.getElementById("cancelChangePasswordButton");
const forcedSignOutButton = document.getElementById("forcedSignOutButton");
const userManagementStatus = document.getElementById("userManagementStatus");
const documentGenerationForm = document.getElementById("documentGenerationForm");
const documentGenerationTitleInput = document.getElementById("documentGenerationTitleInput");
const documentGenerationFormatSelect = document.getElementById("documentGenerationFormatSelect");
const documentGenerationInstructionsInput = document.getElementById("documentGenerationInstructionsInput");
const documentGenerationStatus = document.getElementById("documentGenerationStatus");
const generateDocumentButton = document.getElementById("generateDocumentButton");
const documentBrowser = document.getElementById("documentBrowser");
const browserTitle = document.getElementById("browserTitle");
const closeBrowserButton = document.getElementById("closeBrowserButton");
const browserPreviewPanel = document.getElementById("browserPreviewPanel");
const previewPanelTitle = document.getElementById("previewPanelTitle");
const previewZoomLabel = document.getElementById("previewZoomLabel");
const minimizePreviewButton = document.getElementById("minimizePreviewButton");
const closePreviewButton = document.getElementById("closePreviewButton");
const previewContent = document.getElementById("previewContent");
const browserStats = document.getElementById("browserStats");
const scopeInventorySummary = document.getElementById("scopeInventorySummary");
const scopeAppliedSummary = document.getElementById("scopeAppliedSummary");
const scopeDraftSummary = document.getElementById("scopeDraftSummary");
const scopeIncludedList = document.getElementById("scopeIncludedList");
const scopeExcludedList = document.getElementById("scopeExcludedList");
const folderTreeList = document.getElementById("folderTreeList");
const folderTreeSurface = folderTreeList.parentElement;
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
const previewSourceMedia = document.getElementById("previewSourceMedia");
const previewText = document.getElementById("previewText");
const documentEditorEmpty = document.getElementById("documentEditorEmpty");
const documentEditorForm = document.getElementById("documentEditorForm");
const documentEditorCard = documentEditorForm.closest(".document-editor-card");
documentEditorCard.insertBefore(browserPreviewPanel, documentEditorForm);
const documentEditorId = document.getElementById("documentEditorId");
const documentEditorTitleInput = document.getElementById("documentEditorTitleInput");
const documentEditorCategoryInput = document.getElementById("documentEditorCategoryInput");
const documentEditorFolderInput = document.getElementById("documentEditorFolderInput");
const documentEditorTagsInput = document.getElementById("documentEditorTagsInput");
const documentEditorTagChips = document.getElementById("documentEditorTagChips");
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
const openSourceLocationButton = document.getElementById("openSourceLocationButton");
const renameSelectedFolderButton = document.getElementById("renameSelectedFolderButton");
const syncSelectedFolderButton = document.getElementById("syncSelectedFolderButton");
const closeFolderPropertiesButton = document.getElementById("closeFolderPropertiesButton");
const deleteSelectionSummary = document.getElementById("deleteSelectionSummary");
const deleteChipList = document.getElementById("deleteChipList");
const deleteProgressPanel = document.getElementById("deleteProgressPanel");
const deleteProgressTrack = document.getElementById("deleteProgressTrack");
const deleteProgressBar = document.getElementById("deleteProgressBar");
const deleteProgressLabel = document.getElementById("deleteProgressLabel");
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
const imageLightbox = document.getElementById("imageLightbox");
const imageLightboxBackdrop = document.getElementById("imageLightboxBackdrop");
const imageLightboxCloseButton = document.getElementById("imageLightboxCloseButton");
const imageLightboxImage = document.getElementById("imageLightboxImage");
const imageLightboxCaption = document.getElementById("imageLightboxCaption");

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
// Upload parsing and duplicate resolution ----------------------------------
// Binary Office/PDF formats must be base64 encoded before entering JSON.
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

function createTagChip(tag, { className = "", removable = false, onRemove, title = "" } = {}) {
  const chip = document.createElement("span");
  chip.className = `tag-chip ${className}`.trim();
  if (title) {
    chip.title = title;
  }

  const label = document.createElement("span");
  label.className = "tag-chip-label";
  label.textContent = tag;
  chip.appendChild(label);

  if (removable && onRemove) {
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "tag-chip-remove";
    removeButton.setAttribute("aria-label", `Remove tag ${tag}`);
    removeButton.title = `Remove ${tag}`;
    removeButton.textContent = "×";
    removeButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      onRemove(tag);
    });
    chip.appendChild(removeButton);
  }

  return chip;
}

function renderDocumentEditorTags() {
  if (!documentEditorTagChips) {
    return;
  }

  const tags = normalizeTagItems(documentEditorTagsInput.value || "");
  documentEditorTagChips.innerHTML = "";
  if (tags.length === 0) {
    const empty = document.createElement("span");
    empty.className = "tag-chip-empty";
    empty.textContent = "No manual tags";
    documentEditorTagChips.appendChild(empty);
    return;
  }

  const canRemove = canMutateLibrary() && Boolean(getPreviewDocument()) && !state.library.metadataUpdateInFlight;
  tags.forEach((tag) => {
    documentEditorTagChips.appendChild(createTagChip(tag, {
      className: "document-editor-tag",
      removable: canRemove,
      onRemove: removeDocumentEditorTag,
    }));
  });
}

function removeDocumentEditorTag(tagToRemove) {
  const nextTags = normalizeTagItems(documentEditorTagsInput.value || "")
    .filter((tag) => tag.toLowerCase() !== tagToRemove.toLowerCase());
  documentEditorTagsInput.value = nextTags.join(", ");
  state.library.editorDirty = true;
  renderDocumentEditorTags();
  setDocumentEditorStatus(`Removed “${tagToRemove}”. Save changes to apply it.`);
  documentEditorTagsInput.focus();
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

// Conversation-local preferences -------------------------------------------
// Preferences and scroll positions survive reloads in bounded localStorage maps.
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
    sourceMode: preferences && preferences.sourceMode === "internal" ? "internal" : "broader",
    reasoningMode:
      preferences && preferences.reasoningMode === "maximum" ? "maximum" : "standard",
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
    reasoningMode: state.reasoningMode,
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

function getDefaultConversationPreferences() {
  return normalizeConversationPreferences({
    sourceMode: "broader",
    reasoningMode: "standard",
    contextFilter: { folderIds: [], documentIds: [] },
  });
}

function resolveSavedConversationPreferences(payload) {
  const serverPreferences = normalizeConversationPreferences({
    sourceMode: payload.source_mode,
    reasoningMode: payload.reasoning_mode,
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

function normalizeConversationScrollPosition(position) {
  return {
    scrollTop: Math.max(0, Number(position && position.scrollTop) || 0),
    atBottom: Boolean(position && position.atBottom),
    updatedAt: Math.max(0, Number(position && position.updatedAt) || 0),
  };
}

function readConversationScrollStore() {
  try {
    const payload = JSON.parse(window.localStorage.getItem(CHAT_SCROLL_STORAGE_KEY) || "null");
    if (!payload || typeof payload !== "object") {
      return {};
    }
    return Object.fromEntries(
      Object.entries(payload).map(([conversationId, position]) => [
        conversationId,
        normalizeConversationScrollPosition(position),
      ])
    );
  } catch {
    return {};
  }
}

function writeConversationScrollStore(store) {
  try {
    const positions = Object.fromEntries(
      Object.entries(store || {})
        .map(([conversationId, position]) => [
          conversationId,
          normalizeConversationScrollPosition(position),
        ])
        .sort((left, right) => right[1].updatedAt - left[1].updatedAt)
        .slice(0, MAX_STORED_CHAT_SCROLL_POSITIONS)
    );
    window.localStorage.setItem(CHAT_SCROLL_STORAGE_KEY, JSON.stringify(positions));
  } catch {
    // Browser storage can be unavailable. The chat still defaults to its latest message.
  }
}

function rememberConversationScrollPosition(conversationId) {
  if (!conversationId || state.memory.restoringScrollPosition) {
    return;
  }
  const maximumScrollTop = Math.max(0, messageList.scrollHeight - messageList.clientHeight);
  const store = readConversationScrollStore();
  store[conversationId] = {
    scrollTop: Math.min(maximumScrollTop, Math.max(0, messageList.scrollTop)),
    atBottom: maximumScrollTop - messageList.scrollTop <= 24,
    updatedAt: Date.now(),
  };
  writeConversationScrollStore(store);
}

function rememberActiveConversationScrollPosition() {
  if (conversationScrollSaveTimer !== null) {
    window.clearTimeout(conversationScrollSaveTimer);
    conversationScrollSaveTimer = null;
  }
  rememberConversationScrollPosition(state.conversationId);
}

function restoreConversationScrollPosition(conversationId) {
  const storedPosition = readConversationScrollStore()[conversationId];
  const maximumScrollTop = Math.max(0, messageList.scrollHeight - messageList.clientHeight);
  state.memory.restoringScrollPosition = true;
  messageList.scrollTop = !storedPosition || storedPosition.atBottom
    ? maximumScrollTop
    : Math.min(maximumScrollTop, storedPosition.scrollTop);
  window.requestAnimationFrame(() => {
    state.memory.restoringScrollPosition = false;
  });
}

function forgetConversationScrollPosition(conversationId) {
  const store = readConversationScrollStore();
  delete store[conversationId];
  writeConversationScrollStore(store);
}

async function syncConversationPreferences(conversationId, preferences) {
  if (!state.auth.user || state.auth.user.must_change_password) {
    return;
  }
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
        reasoning_mode: normalized.reasoningMode,
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
  if (!state.auth.user || state.auth.user.must_change_password) {
    return;
  }
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
    const folderRecord = state.library.folders.find(
      (folder) => normalizeFolderPath(folder.folder_id) === normalizeFolderPath(folderId)
    );
    const safeAlias = (folderRecord?.aliases || [])
      .map((value) => String(value || "").trim())
      .find(Boolean);
    if (safeAlias) {
      return safeAlias;
    }
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

// Library tree interaction --------------------------------------------------
// Drag payloads live in state as a fallback for browsers with restricted MIME data.
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

function expandFolderAndDescendants(pathId) {
  const normalizedPathId = normalizeFolderPath(pathId);
  if (!normalizedPathId) {
    return;
  }

  const descendantIds = new Set([normalizedPathId]);
  getAllLibraryFolderPathIds().forEach((folderId) => {
    if (folderId === normalizedPathId || folderId.startsWith(`${normalizedPathId}/`)) {
      descendantIds.add(folderId);
    }
  });

  state.library.collapsedFolderIds = normalizeItems(
    state.library.collapsedFolderIds.filter((folderId) => !descendantIds.has(folderId))
  );
  renderFolderTree();
}

function handleFolderControlClick(event) {
  if (event.detail === 0) {
    return;
  }

  const folderMain = event.target.closest(".folder-tree-main");
  if (!folderMain || !folderTreeList.contains(folderMain)) {
    return;
  }

  const folderRow = folderMain.closest(".folder-tree-row");
  const folderId = normalizeFolderPath(folderRow ? folderRow.dataset.folderId : "");
  if (!folderId) {
    return;
  }

  const control = event.target.closest(".folder-expand-button") ? "toggle" : "entry";
  const clickedAt = window.performance.now();
  const elapsed = clickedAt - lastFolderControlClick.clickedAt;
  const pointerDistance = Math.hypot(
    event.clientX - lastFolderControlClick.x,
    event.clientY - lastFolderControlClick.y
  );
  const isDoubleClick =
    lastFolderControlClick.folderId === folderId &&
    lastFolderControlClick.control === control &&
    elapsed > 0 &&
    elapsed <= FOLDER_DOUBLE_CLICK_WINDOW_MS &&
    pointerDistance <= 16;

  lastFolderControlClick = isDoubleClick
    ? {
        folderId: null,
        control: null,
        clickedAt: 0,
        x: 0,
        y: 0,
      }
    : {
        folderId,
        control,
        clickedAt,
        x: event.clientX,
        y: event.clientY,
      };

  if (!isDoubleClick) {
    return;
  }

  event.preventDefault();
  event.stopImmediatePropagation();
  expandFolderAndDescendants(folderId);
}

function getAllLibraryFolderPathIds() {
  const pathIds = [];
  state.library.folders.forEach((item) => {
    let currentPath = "";
    getFolderSegments(item.folder_id).forEach((segment) => {
      currentPath = currentPath ? `${currentPath}/${segment}` : segment;
      pathIds.push(currentPath);
    });
  });
  return normalizeItems(pathIds);
}

function setAllFoldersCollapsed(isCollapsed) {
  if (!isCollapsed) {
    state.library.collapsedFolderIds = [];
  } else {
    state.library.collapsedFolderIds = getAllLibraryFolderPathIds();
  }
  renderFolderTree();
}

function setActiveFolder(pathId, options = {}) {
  const { renderTree = true } = options;
  state.library.activeFolderId = pathId;
  state.library.previewDocumentId = null;
  state.library.editorDismissed = false;
  renderBrowserStats();
  if (renderTree) {
    renderFolderTree();
  } else {
    folderTreeList.querySelectorAll(".folder-tree-row").forEach((row) => {
      const rowFolderId = row.dataset.folderId || null;
      row.classList.toggle("is-active", rowFolderId === state.library.activeFolderId);
    });
  }
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

// Authentication and account UI --------------------------------------------
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

function canManageLibrary() {
  return ["admin", "library_manager"].includes(state.auth.user?.role);
}

function canMutateLibrary() {
  return (
    canManageLibrary() &&
    (state.library.backend === "json" || state.library.backend === "semantic")
  );
}

function stripGeneratedUploadCitations(value) {
  return String(value || "")
    .replace(/[ \t]*\[?UPL-[A-Za-z0-9][A-Za-z0-9_-]*\]?/gi, "")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
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

function formatUserRole(role) {
  if (role === "admin") {
    return "Administrator";
  }
  if (role === "library_manager") {
    return "Library manager";
  }
  return "Member";
}

function setUserManagementStatus(message, tone = "neutral") {
  userManagementStatus.textContent = message;
  userManagementStatus.dataset.tone = tone;
}

function authInitials(user) {
  return String(user?.display_name || user?.username || "U")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase() || "U";
}

function applyAuthenticationGate() {
  const authenticated =
    Boolean(state.auth.user) && !state.auth.user.must_change_password;
  newConversationButton.disabled = !authenticated;
  conversationSearchInput.disabled = !authenticated;
  openLibraryButton.disabled = !authenticated;
  openRoutinesButton.disabled = !authenticated;
  if (openSynchronizedPathsButton) {
    openSynchronizedPathsButton.disabled = !authenticated || !canManageLibrary();
  }
  messageInput.placeholder = authenticated
    ? "Ask about policies, documents, onboarding, billing, support, or general questions."
    : "Sign in to start a private conversation.";
  setComposerState(state.sending);
  renderSavedConversationList();
  renderConversationSaveButton();
  updateConversationMemoryStatus();
}

function clearConversationWorkspaceForAuth() {
  state.memory.conversations = [];
  state.memory.loaded = true;
  state.memory.loadError = null;
  state.memory.searchQuery = "";
  conversationSearchInput.value = "";
  resetConversation({ rememberCurrentScroll: false });
  applyAuthenticationGate();
}

function clearLibraryWorkspaceForAuth() {
  closeDocumentBrowser();
  state.library.backend = null;
  state.library.totalDocuments = 0;
  state.library.totalChunks = null;
  state.library.folders = [];
  state.library.documents = [];
  state.library.loaded = false;
  state.library.loadError = null;
  state.library.activeFolderId = null;
  state.library.searchQuery = "";
  state.library.previewDocumentId = null;
  state.library.previewCache = {};
  state.library.deleteSelectionIds = [];
  state.library.editorDocumentId = null;
  state.library.watchFolders = [];
  state.library.watchFoldersLoaded = false;
  state.library.watchFoldersLoadError = null;
  if (previewText) {
    previewText.textContent = "";
  }
  if (folderPropertiesSource) {
    folderPropertiesSource.textContent = "";
  }
}

function clearRoutineWorkspaceForAuth() {
  closeRoutines();
  state.routines.items = [];
  state.routines.runs = [];
  state.routines.policy = {};
  state.routines.systemPaused = false;
  state.routines.loaded = false;
  state.routines.inFlight = false;
  state.routines.draftContext = { folderIds: [], documentIds: [] };
  state.routines.draftSourceMode = "internal";
  state.routines.scopePickerOpen = false;
  state.routines.scopeSearchQuery = "";
  state.routines.scopeCollapsedFolderIds = [];
  state.routines.scopeTreeInitialized = false;
  renderRoutines();
}

async function activateAuthenticatedConversationWorkspace() {
  if (!state.auth.user || state.auth.user.must_change_password) {
    clearConversationWorkspaceForAuth();
    return;
  }
  state.memory.conversations = [];
  state.memory.loaded = false;
  state.memory.loadError = null;
  resetConversation({ rememberCurrentScroll: false });
  applyAuthenticationGate();
  await loadSavedConversations({ openMostRecent: true });
}

function renderAuth() {
  const user = state.auth.user;
  const authenticated = Boolean(user);
  const forcedChange = Boolean(user?.must_change_password);
  if (forcedChange) {
    state.auth.view = "change";
  }

  userLoginLabel.textContent = authenticated ? user.display_name : "Sign in";
  userLoginBadge.setAttribute(
    "aria-label",
    authenticated ? `Open account for ${user.display_name}` : "Sign in or create an account"
  );
  userLoginBadge.title = userLoginBadge.getAttribute("aria-label");

  const signedOut = !authenticated;
  authSignedOutView.classList.toggle("is-hidden", !signedOut);
  authAccountView.classList.toggle(
    "is-hidden",
    signedOut || state.auth.view !== "account"
  );
  changePasswordForm.classList.toggle(
    "is-hidden",
    signedOut || state.auth.view !== "change"
  );

  const showingSignup = state.auth.view === "signup";
  showSignInButton.classList.toggle("is-active", !showingSignup);
  showSignInButton.setAttribute("aria-selected", String(!showingSignup));
  showSignUpButton.classList.toggle("is-active", showingSignup);
  showSignUpButton.setAttribute("aria-selected", String(showingSignup));
  signInForm.classList.toggle("is-hidden", showingSignup);
  signUpForm.classList.toggle("is-hidden", !showingSignup);

  if (authenticated) {
    accountAvatar.textContent = authInitials(user);
    accountDisplayName.textContent = user.display_name;
    accountUsername.textContent = `@${user.username}`;
    accountRole.textContent = formatUserRole(user.role);
  }

  closeUserManagementButton.disabled = forcedChange;
  closeUserManagementButton.classList.toggle("is-hidden", forcedChange);
  cancelChangePasswordButton.classList.toggle("is-hidden", forcedChange);
  forcedSignOutButton.classList.toggle("is-hidden", !forcedChange);
  changePasswordTitle.textContent = forcedChange
    ? "Set a new password"
    : "Change your password";
  changePasswordGuidance.textContent = forcedChange
    ? "For security, replace the temporary Administrator password before continuing."
    : "Enter your current password and choose a new one.";

  if (signedOut) {
    authEyebrow.textContent = "Your account";
    userManagementTitle.textContent = showingSignup ? "Create your account" : "Welcome back";
    authDescription.textContent = showingSignup
      ? "Sign up for your own conversations and access to the shared library."
      : "Sign in to access your account.";
  } else {
    authEyebrow.textContent = forcedChange ? "First-time setup" : "Your account";
    userManagementTitle.textContent = forcedChange
      ? "Secure the Administrator account"
      : user.display_name;
    authDescription.textContent = forcedChange
      ? "The temporary password must be changed before you continue."
      : "Review your account or update your password.";
  }
  applyAuthenticationGate();
}

async function loadAuthSession() {
  try {
    const response = await fetch("/api/auth/session", { cache: "no-store" });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(payload?.detail || "Could not load your account.");
    }
    state.auth.user = payload?.authenticated ? payload.user : null;
    state.auth.view = state.auth.user
      ? (state.auth.user.must_change_password ? "change" : "account")
      : "signin";
  } catch (error) {
    state.auth.user = null;
    setUserManagementStatus(error.message, "error");
  }
  state.auth.loaded = true;
  renderAuth();
}

async function initializeAuthenticatedWorkspace() {
  await loadAuthSession();
  if (state.auth.user && !state.auth.user.must_change_password) {
    const params = new URLSearchParams(window.location.search);
    if (params.get("routines_popup") === "1") {
      const requestedRoutineId = String(params.get("routine_id") || "");
      if (requestedRoutineId) {
        void openRoutineEditor(requestedRoutineId);
      } else {
        void openRoutines();
      }
    }
    await activateAuthenticatedConversationWorkspace();
  } else {
    clearConversationWorkspaceForAuth();
    clearLibraryWorkspaceForAuth();
    clearRoutineWorkspaceForAuth();
  }
}

function openUserManagement(returnFocus = userLoginBadge) {
  closeSavedConversationContextMenu();
  userManagementReturnFocus = returnFocus;
  state.auth.view = state.auth.user
    ? (state.auth.user.must_change_password ? "change" : "account")
    : "signin";
  renderAuth();
  userManagementModal.classList.remove("is-hidden");
  userManagementModal.setAttribute("aria-hidden", "false");
  setUserManagementStatus("", "neutral");
  window.requestAnimationFrame(() => {
    if (state.auth.view === "signin") {
      signInUsernameInput.focus();
    } else if (state.auth.view === "change") {
      currentPasswordInput.focus();
    } else {
      showChangePasswordButton.focus();
    }
  });
}

function closeUserManagement() {
  if (state.auth.user?.must_change_password) {
    setUserManagementStatus("Change the temporary password or sign out.", "error");
    currentPasswordInput.focus();
    return;
  }
  userManagementModal.classList.add("is-hidden");
  userManagementModal.setAttribute("aria-hidden", "true");
  const returnFocus = userManagementReturnFocus || userLoginBadge;
  userManagementReturnFocus = null;
  returnFocus.focus();
}

function setAuthView(view) {
  state.auth.view = view;
  setUserManagementStatus("", "neutral");
  renderAuth();
  window.requestAnimationFrame(() => {
    if (view === "signup") {
      signUpDisplayNameInput.focus();
    } else if (view === "signin") {
      signInUsernameInput.focus();
    } else if (view === "change") {
      currentPasswordInput.focus();
    }
  });
}

async function submitAuth(endpoint, body, button, busyLabel) {
  if (state.auth.inFlight) {
    return;
  }
  state.auth.inFlight = true;
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = busyLabel;
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(payload?.detail || "Account request failed.");
    }
    state.auth.user = payload.user || null;
    state.auth.view = state.auth.user?.must_change_password ? "change" : "account";
    renderAuth();
    if (state.auth.user && !state.auth.user.must_change_password) {
      await activateAuthenticatedConversationWorkspace();
    } else {
      clearConversationWorkspaceForAuth();
    }
    setUserManagementStatus(payload.message || "Account updated.", "success");
    return true;
  } catch (error) {
    setUserManagementStatus(error.message, "error");
    return false;
  } finally {
    state.auth.inFlight = false;
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

async function signOut() {
  if (state.auth.inFlight) {
    return;
  }
  state.auth.inFlight = true;
  try {
    const response = await fetch("/api/auth/logout", { method: "POST" });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(payload?.detail || "Could not sign out.");
    }
    state.auth.user = null;
    state.auth.view = "signin";
    renderAuth();
    clearConversationWorkspaceForAuth();
    clearLibraryWorkspaceForAuth();
    clearRoutineWorkspaceForAuth();
    setUserManagementStatus(payload.message || "Signed out.", "success");
    signInPasswordInput.value = "";
    window.requestAnimationFrame(() => signInUsernameInput.focus());
  } catch (error) {
    setUserManagementStatus(error.message, "error");
  } finally {
    state.auth.inFlight = false;
  }
}

// Bounded routines ---------------------------------------------------------
function setRoutineStatus(message, tone = "neutral") {
  routineStatus.textContent = message || "";
  routineStatus.dataset.tone = tone;
}

function activeRoutineScope() {
  return {
    folder_ids: [...state.routines.draftContext.folderIds],
    document_ids: [...state.routines.draftContext.documentIds],
  };
}

function renderRoutineScopeSummary() {
  const isGlobal = state.routines.draftSourceMode === "broader";
  const folders = state.routines.draftContext.folderIds.length;
  const documents = state.routines.draftContext.documentIds.length;
  const selected = folders + documents;
  routineScopeSummary.textContent = isGlobal
    ? selected
      ? `Global context is active. ${folders} folder${folders === 1 ? "" : "s"} and ${documents} file${documents === 1 ? "" : "s"} are included, with one limited public web search available.`
      : "Global context is active. This routine may use one limited public web search; adding a document scope is optional."
    : selected
    ? `${folders} folder${folders === 1 ? "" : "s"} and ${documents} file${documents === 1 ? "" : "s"} selected for this routine only.`
    : "No routine scope selected. It is independent from the current chat scope.";
  routineScopeSelectionList.replaceChildren();
  state.routines.draftContext.folderIds.forEach((folderId) => {
    renderScopePill(
      routineScopeSelectionList,
      `Folder: ${formatFolderDisplayPath(folderId)}`,
      () => setRoutineScopeSelected("folder", folderId, false)
    );
  });
  state.routines.draftContext.documentIds.forEach((documentId) => {
    const documentSummary = getDocumentSummary(documentId);
    renderScopePill(
      routineScopeSelectionList,
      documentSummary ? getDocumentChipLabel(documentSummary) : `Doc: ${documentId}`,
      () => setRoutineScopeSelected("document", documentId, false)
    );
  });
  routineScopePickerButton.disabled = state.routines.inFlight;
  createRoutineButton.disabled = state.routines.inFlight || (!isGlobal && selected === 0);
}

function applyRoutineSourceMode(sourceMode) {
  state.routines.draftSourceMode = sourceMode === "broader" ? "broader" : "internal";
  const isGlobal = state.routines.draftSourceMode === "broader";
  routineSourceModeButton.classList.toggle("is-broader", isGlobal);
  routineSourceModeButton.setAttribute("aria-pressed", String(isGlobal));
  routineSourceModeButton.setAttribute("aria-label", isGlobal ? "Context: Global" : "Context: Internal");
  routineSourceModeButton.title = isGlobal
    ? "Context: Global is active. Click for Context: Internal."
    : "Context: Internal is active. Click for Context: Global.";
  routineSourceModeLabel.textContent = isGlobal ? "Context: Global" : "Context: Internal";
}

function routineScopeSelectionCount() {
  return (
    state.routines.draftContext.folderIds.length +
    state.routines.draftContext.documentIds.length
  );
}

function isRoutineScopeSelected(kind, id) {
  return kind === "folder"
    ? state.routines.draftContext.folderIds.includes(id)
    : state.routines.draftContext.documentIds.includes(id);
}

function getRoutineFolderAliases(folderId) {
  const normalizedFolderId = normalizeFolderPath(folderId);
  if (!normalizedFolderId) {
    return [];
  }
  return normalizeItems(
    [
      ...(state.library.folders.find(
        (folder) => normalizeFolderPath(folder.folder_id) === normalizedFolderId
      )?.aliases || []),
      ...state.library.watchFolders
        .filter(
          (watchFolder) =>
            getResolvedWatchedLibraryFolder(watchFolder) === normalizedFolderId
        )
        .flatMap((watchFolder) => [watchFolder.alias, watchFolder.display_name]),
    ]
      .map((value) => String(value || "").trim())
      .filter(Boolean)
  );
}

function isRoutineScopeFolderCollapsed(folderId) {
  return state.routines.scopeCollapsedFolderIds.includes(normalizeFolderPath(folderId));
}

function toggleRoutineScopeFolderCollapsed(folderId) {
  const normalizedFolderId = normalizeFolderPath(folderId);
  const collapsed = new Set(state.routines.scopeCollapsedFolderIds);
  if (collapsed.has(normalizedFolderId)) {
    collapsed.delete(normalizedFolderId);
  } else {
    collapsed.add(normalizedFolderId);
  }
  state.routines.scopeCollapsedFolderIds = normalizeItems(Array.from(collapsed));
  renderRoutines();
}

function setRoutineScopeSelected(kind, id, selected) {
  const key = kind === "folder" ? "folderIds" : "documentIds";
  const current = state.routines.draftContext[key];
  if (selected && !current.includes(id)) {
    if (routineScopeSelectionCount() >= 20) {
      setRoutineStatus("A routine can include at most 20 folders and files.", "error");
      return;
    }
    state.routines.draftContext[key] = [...current, id];
  } else if (!selected) {
    state.routines.draftContext[key] = current.filter((item) => item !== id);
  }
  renderRoutines();
}

function renderRoutineScopePicker() {
  const pickerOpen = state.routines.scopePickerOpen;
  routineScopePicker.classList.toggle("is-hidden", !pickerOpen);
  routineScopePickerButton.textContent = "Open Library to select scope";
  if (!pickerOpen) {
    return;
  }

  routineScopePickerList.replaceChildren();
  if (!state.library.loaded) {
    const loading = document.createElement("p");
    loading.className = "routine-empty";
    loading.textContent = "Loading library choices...";
    routineScopePickerList.append(loading);
    return;
  }
  if (state.library.loadError) {
    const unavailable = document.createElement("p");
    unavailable.className = "routine-run-error";
    unavailable.textContent = `Library choices are unavailable: ${state.library.loadError}`;
    routineScopePickerList.append(unavailable);
    return;
  }

  const query = state.routines.scopeSearchQuery.trim().toLowerCase();
  const matches = (value) => !query || String(value || "").toLowerCase().includes(query);
  const tree = getFolderTreeNodes();
  let visibleItemCount = 0;

  const documentMatchesSearch = (documentSummary) =>
    matches(
      [
        documentSummary.title,
        documentSummary.folder,
        formatFolderDisplayPath(documentSummary.folder),
        documentSummary.category,
      ].join(" ")
    );

  const renderDocumentRow = (documentSummary, depth) => {
    const includedViaFolder = state.routines.draftContext.folderIds.some((folderId) =>
      folderPathContainsFolder(folderId, documentSummary.folder)
    );
    const isDirectlySelected = isRoutineScopeSelected("document", documentSummary.document_id);
    const row = document.createElement("label");
    row.className = "routine-scope-tree-document";
    row.classList.toggle("is-scoped", includedViaFolder || isDirectlySelected);
    row.style.paddingLeft = `${depth * 12 + 30}px`;

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = includedViaFolder || isDirectlySelected;
    checkbox.disabled = state.routines.inFlight || includedViaFolder;
    checkbox.title = includedViaFolder
      ? "Included by the selected parent folder"
      : "Include this file only";
    checkbox.addEventListener("change", () => {
      setRoutineScopeSelected("document", documentSummary.document_id, checkbox.checked);
    });

    const copy = document.createElement("span");
    copy.className = "routine-scope-tree-copy";
    const name = document.createElement("strong");
    name.textContent = documentSummary.title || getDocumentDisplayLabel(documentSummary);
    name.title = getDocumentDisplayLabel(documentSummary);
    const meta = document.createElement("small");
    meta.textContent = documentSummary.category || "Library file";
    copy.append(name, meta);
    row.append(checkbox, copy);
    return row;
  };

  const renderNode = (node) => {
    const folderDocuments = node.pathId ? getFolderDocuments(node.pathId) : [];
    const visibleDocuments = folderDocuments.filter(documentMatchesSearch);
    const children = node.children.map(renderNode).filter(Boolean);
    const aliases = node.pathId ? getRoutineFolderAliases(node.pathId) : [];
    const folderSearchText = node.pathId
      ? [node.pathId, formatFolderDisplayPath(node.pathId), ...aliases].join(" ")
      : node.label;
    const folderMatches = matches(folderSearchText);
    const isVisible = !query || folderMatches || visibleDocuments.length > 0 || children.length > 0;
    if (!isVisible) {
      return null;
    }

    const item = document.createElement("div");
    item.className = "routine-scope-tree-item";
    if (node.pathId) {
      const hasChildren = node.children.length > 0 || folderDocuments.length > 0;
      const directlySelected = node.folder && isRoutineScopeSelected("folder", node.folder.folder_id);
      const selectedByAncestor = node.folder && !directlySelected && state.routines.draftContext.folderIds.some((folderId) =>
        folderPathContainsFolder(folderId, node.folder.folder_id)
      );
      const collapsed = !query && isRoutineScopeFolderCollapsed(node.pathId);
      const row = document.createElement("div");
      row.className = "routine-scope-tree-row";
      row.classList.toggle("is-scoped", Boolean(directlySelected || selectedByAncestor));
      row.style.paddingLeft = `${node.depth * 12}px`;

      const expand = document.createElement("button");
      expand.type = "button";
      expand.className = "routine-scope-expand-button";
      expand.disabled = !hasChildren;
      expand.textContent = hasChildren ? (collapsed ? ">" : "v") : "";
      expand.setAttribute("aria-label", collapsed ? "Expand folder" : "Collapse folder");
      expand.addEventListener("click", () => {
        if (hasChildren) {
          toggleRoutineScopeFolderCollapsed(node.pathId);
        }
      });

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = Boolean(directlySelected || selectedByAncestor);
      checkbox.disabled = state.routines.inFlight || Boolean(selectedByAncestor) || !node.folder;
      checkbox.title = selectedByAncestor
        ? "Included by the selected parent folder"
        : "Include this folder and its contents";
      checkbox.addEventListener("change", () => {
        if (node.folder) {
          setRoutineScopeSelected("folder", node.folder.folder_id, checkbox.checked);
        }
      });

      const copy = document.createElement("span");
      copy.className = "routine-scope-tree-copy";
      const name = document.createElement("strong");
      name.textContent = getFolderDisplayName(node.pathId, node.label);
      name.title = `${formatFolderDisplayPath(node.pathId)} (${formatFolderPath(node.pathId)})`;
      const meta = document.createElement("small");
      const aliasText = aliases.filter((alias) => alias !== name.textContent).join(" · ");
      meta.textContent = aliasText
        ? `Alias: ${aliasText} · ${node.totalDocumentCount} indexed file${node.totalDocumentCount === 1 ? "" : "s"}`
        : `${node.totalDocumentCount} indexed file${node.totalDocumentCount === 1 ? "" : "s"}`;
      copy.append(name, meta);
      row.append(expand, checkbox, copy);
      item.append(row);
      if (!collapsed) {
        children.forEach((child) => item.append(child));
        visibleDocuments.forEach((documentSummary) => item.append(renderDocumentRow(documentSummary, node.depth + 1)));
      }
    } else {
      children.forEach((child) => item.append(child));
    }
    visibleItemCount += 1;
    return item;
  };

  const renderedTree = renderNode(tree);
  if (!renderedTree || visibleItemCount === 0) {
    const empty = document.createElement("p");
    empty.className = "routine-empty";
    empty.textContent = query ? "No folders or files match that search." : "No library folders or files are available.";
    routineScopePickerList.append(empty);
    return;
  }
  routineScopePickerList.append(renderedTree);
}

function openRoutineScopePicker() {
  state.routines.scopePickerOpen = false;
  renderRoutines();
  openDocumentBrowser({ scopeTarget: "routine" });
}

function formatRoutineDate(value) {
  if (!value) {
    return "Not yet";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function routineScheduleLabel(routine) {
  const time = `${String(routine.schedule_hour).padStart(2, "0")}:${String(routine.schedule_minute).padStart(2, "0")}`;
  if (routine.schedule_kind === "weekly") {
    const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
    return `${weekdays[routine.schedule_weekday] || "Weekly"} at ${time} (${routine.timezone})`;
  }
  return `Daily at ${time} (${routine.timezone})`;
}

function appendRoutineAction(label, className, handler, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener("click", handler);
  return button;
}

function renderRoutines() {
  renderRoutineScopeSummary();
  renderRoutineScopePicker();
  const policy = state.routines.policy || {};
  routinePolicySummary.textContent = state.routines.loaded
    ? `Up to ${policy.max_runs_per_user_daily || 0} runs/day and ${policy.max_runs_per_user_monthly || 0} runs/month per user; ${policy.max_documents || 0} documents/run; ${policy.run_retention_days || 0}-day result retention. Failed runs count toward limits.`
    : "Routine limits are enforced by the server before every model request.";
  routineSystemPauseButton.classList.toggle("is-hidden", state.auth.user?.role !== "admin");
  routineSystemPauseButton.textContent = state.routines.systemPaused ? "Resume all" : "Pause all";
  routineSystemPauseButton.disabled = state.routines.inFlight;

  routineList.replaceChildren();
  if (!state.routines.loaded) {
    const loading = document.createElement("p");
    loading.className = "routine-empty";
    loading.textContent = "Loading routines...";
    routineList.append(loading);
  } else if (!state.routines.items.length) {
    const empty = document.createElement("p");
    empty.className = "routine-empty";
    empty.textContent = "No routines yet. Create one with its own internal library scope.";
    routineList.append(empty);
  } else {
    state.routines.items.forEach((routine) => {
      const card = document.createElement("article");
      card.className = `routine-card${routine.enabled ? "" : " is-paused"}`;
      const head = document.createElement("div");
      head.className = "routine-card-head";
      const titleWrap = document.createElement("div");
      const title = document.createElement("h4");
      title.textContent = routine.name;
      const schedule = document.createElement("p");
      schedule.textContent = routineScheduleLabel(routine);
      titleWrap.append(title, schedule);
      const badge = document.createElement("span");
      badge.className = `routine-state-pill${routine.enabled ? "" : " is-paused"}`;
      badge.textContent = routine.enabled ? "Active" : "Paused";
      head.append(titleWrap, badge);
      const instructions = document.createElement("p");
      instructions.className = "routine-instructions";
      instructions.textContent = routine.instructions;
      const meta = document.createElement("p");
      meta.className = "routine-meta";
      meta.textContent = `Next: ${formatRoutineDate(routine.next_run_at)} · ${String(routine.output_format).toUpperCase()} · Failures: ${routine.consecutive_failures}`;
      const actions = document.createElement("div");
      actions.className = "routine-card-actions";
      actions.append(
        appendRoutineAction("Run now", "primary-button compact-button", () => void runRoutineNow(routine.routine_id), state.routines.inFlight || state.routines.systemPaused),
        appendRoutineAction("Edit", "secondary-button compact-button", () => void openRoutineEditor(routine.routine_id), state.routines.inFlight),
        appendRoutineAction(routine.enabled ? "Pause" : "Enable", "ghost-button compact-button", () => void toggleRoutine(routine), state.routines.inFlight),
        appendRoutineAction("Delete", "ghost-button compact-button routine-delete-button", () => void deleteRoutine(routine), state.routines.inFlight)
      );
      card.append(head, instructions, meta, actions);
      routineList.append(card);
    });
  }

  routineRunList.replaceChildren();
  if (!state.routines.runs.length) {
    const empty = document.createElement("p");
    empty.className = "routine-empty";
    empty.textContent = "Completed chat responses and generated documents will appear here.";
    routineRunList.append(empty);
    return;
  }
  state.routines.runs.forEach((run) => {
    const card = document.createElement("article");
    card.className = `routine-run-card is-${run.status}`;
    const head = document.createElement("div");
    head.className = "routine-card-head";
    const title = document.createElement("h4");
    title.textContent = run.routine_name;
    const badge = document.createElement("span");
    badge.className = "routine-state-pill";
    badge.textContent = run.status;
    head.append(title, badge);
    const meta = document.createElement("p");
    meta.className = "routine-meta";
    meta.textContent = `${formatRoutineDate(run.started_at)} · ${run.trigger}${run.total_tokens != null ? ` · ${run.total_tokens} tokens` : ""}`;
    card.append(head, meta);
    if (run.response_text) {
      const response = document.createElement("p");
      response.className = "routine-run-response";
      response.textContent = run.response_text;
      card.append(response);
    }
    if (run.error_code) {
      const error = document.createElement("p");
      error.className = "routine-run-error";
      error.textContent = `Run failed: ${run.error_code}. No automatic retry was made.`;
      card.append(error);
    }
    if (run.has_document) {
      const download = document.createElement("a");
      download.className = "secondary-button compact-button routine-download";
      download.href = `/api/routines/runs/${encodeURIComponent(run.run_id)}/document`;
      download.textContent = `Download ${run.filename || "document"}`;
      card.append(download);
    }
    appendRoutineOutputActions(card, run.run_id);
    routineRunList.append(card);
  });
}

function appendRoutineOutputActions(card, runId) {
  card.classList.add("routine-run-card-actions-menu");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "message-actions-button";
  button.setAttribute("aria-label", "Routine output actions");
  button.setAttribute("aria-expanded", "false");
  button.textContent = "⋯";
  const menu = document.createElement("div");
  menu.className = "message-actions-menu is-hidden";
  menu.setAttribute("role", "menu");
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "message-actions-menu-item message-actions-menu-item-danger";
  remove.setAttribute("role", "menuitem");
  remove.textContent = "Delete routine output";
  remove.addEventListener("click", () => {
    menu.classList.add("is-hidden");
    button.setAttribute("aria-expanded", "false");
    void deleteRoutineRunOutput(runId);
  });
  menu.append(remove);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const hidden = menu.classList.toggle("is-hidden");
    button.setAttribute("aria-expanded", String(!hidden));
  });
  card.append(button, menu);
}

async function routineApi(endpoint, options = {}) {
  const response = await fetch(endpoint, {
    cache: "no-store",
    ...options,
    headers: options.body ? { "Content-Type": "application/json", ...(options.headers || {}) } : options.headers,
  });
  const payload = await parseJsonResponse(response);
  if (!response.ok) {
    throw new Error(typeof payload?.detail === "string" ? payload.detail : "Routine request failed.");
  }
  return payload;
}

async function loadRoutines() {
  try {
    const payload = await routineApi("/api/routines");
    state.routines.items = Array.isArray(payload.routines) ? payload.routines : [];
    state.routines.runs = Array.isArray(payload.runs) ? payload.runs : [];
    state.routines.policy = payload.policy || {};
    state.routines.systemPaused = Boolean(payload.system_paused);
    state.routines.loaded = true;
    renderRoutines();
  } catch (error) {
    state.routines.loaded = true;
    renderRoutines();
    setRoutineStatus(error.message, "error");
  }
}

async function openRoutines(returnFocus = openRoutinesButton) {
  if (!state.auth.user || state.auth.user.must_change_password) {
    openUserManagement(returnFocus);
    return;
  }
  closeSavedConversationContextMenu();
  routinesReturnFocus = returnFocus;
  routinesModal.classList.remove("is-hidden");
  routinesModal.setAttribute("aria-hidden", "false");
  setRoutineStatus("", "neutral");
  renderRoutines();
  await loadRoutines();
  window.requestAnimationFrame(() => routineNameInput.focus());
}

function resetRoutineEditor() {
  state.routines.editingId = null;
  routineForm.reset();
  applyRoutineSourceMode("internal");
  routineTimeInput.value = "09:00";
  routineWeekdayField.classList.add("is-hidden");
  state.routines.draftContext = { folderIds: [], documentIds: [] };
  state.routines.scopeSearchQuery = "";
  state.routines.scopePickerOpen = false;
  state.routines.scopeCollapsedFolderIds = [];
  state.routines.scopeTreeInitialized = false;
  routineEditorEyebrow.textContent = "New routine";
  routineEditorHeading.textContent = "Create a recurring task";
  createRoutineButton.textContent = "Create routine";
  resetRoutineEditorButton.classList.add("is-hidden");
}

async function openRoutineEditor(routineId) {
  window.focus();
  await openRoutines();
  const routine = state.routines.items.find((item) => item.routine_id === routineId);
  if (!routine) {
    setRoutineStatus("That routine is no longer available.", "error");
    return;
  }
  state.routines.editingId = routine.routine_id;
  routineNameInput.value = routine.name || "";
  routineInstructionsInput.value = routine.instructions || "";
  routineOutputSelect.value = routine.output_format || "chat";
  routineScheduleSelect.value = routine.schedule_kind === "weekly" ? "weekly" : "daily";
  routineTimeInput.value = `${String(routine.schedule_hour).padStart(2, "0")}:${String(routine.schedule_minute).padStart(2, "0")}`;
  routineWeekdaySelect.value = String(routine.schedule_weekday ?? 0);
  routineWeekdayField.classList.toggle("is-hidden", routineScheduleSelect.value !== "weekly");
  state.routines.draftContext = {
    folderIds: [...(routine.context_filter?.folder_ids || [])],
    documentIds: [...(routine.context_filter?.document_ids || [])],
  };
  applyRoutineSourceMode(routine.source_mode === "broader" ? "broader" : "internal");
  routineEditorEyebrow.textContent = "Edit routine";
  routineEditorHeading.textContent = "Update this recurring task";
  createRoutineButton.textContent = "Save changes";
  resetRoutineEditorButton.classList.remove("is-hidden");
  setRoutineStatus("Editing routine. Changes take effect on its next run.", "neutral");
  renderRoutines();
  window.requestAnimationFrame(() => {
    window.focus();
    routineNameInput.focus();
  });
}

function closeRoutines() {
  if (!routinesModal || routinesModal.classList.contains("is-hidden")) {
    return;
  }
  if (new URLSearchParams(window.location.search).get("routines_popup") === "1" && window.opener && !window.opener.closed) {
    window.close();
    return;
  }
  routinesModal.classList.add("is-hidden");
  routinesModal.setAttribute("aria-hidden", "true");
  resetRoutineEditor();
  const returnFocus = routinesReturnFocus;
  routinesReturnFocus = null;
  if (returnFocus && document.contains(returnFocus)) {
    returnFocus.focus();
  }
}

async function createRoutineFromForm() {
  const scope = activeRoutineScope();
  if (state.routines.draftSourceMode === "internal" && !scope.folder_ids.length && !scope.document_ids.length) {
    setRoutineStatus("Choose at least one internal folder or file first.", "error");
    return;
  }
  const [hourText, minuteText] = String(routineTimeInput.value || "09:00").split(":");
  const scheduleKind = routineScheduleSelect.value === "weekly" ? "weekly" : "daily";
  const body = {
    name: routineNameInput.value.trim(),
    instructions: routineInstructionsInput.value.trim(),
    output_format: routineOutputSelect.value,
    source_mode: state.routines.draftSourceMode,
    schedule_kind: scheduleKind,
    schedule_hour: Number(hourText),
    schedule_minute: Number(minuteText),
    schedule_weekday: scheduleKind === "weekly" ? Number(routineWeekdaySelect.value) : null,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    context_filter: scope,
    enabled: true,
  };
  const editingId = state.routines.editingId;
  const existingRoutine = editingId ? state.routines.items.find((item) => item.routine_id === editingId) : null;
  if (editingId && !existingRoutine) {
    setRoutineStatus("That routine is no longer available.", "error");
    return;
  }
  body.enabled = existingRoutine ? Boolean(existingRoutine.enabled) : true;
  state.routines.inFlight = true;
  renderRoutines();
  setRoutineStatus(editingId ? "Saving routine..." : "Creating routine...", "neutral");
  try {
    const payload = await routineApi(editingId ? `/api/routines/${encodeURIComponent(editingId)}` : "/api/routines", { method: editingId ? "PUT" : "POST", body: JSON.stringify(body) });
    resetRoutineEditor();
    setRoutineStatus(payload.message || (editingId ? "Routine updated." : "Routine created."), "success");
    await loadRoutines();
  } catch (error) {
    setRoutineStatus(error.message, "error");
  } finally {
    state.routines.inFlight = false;
    renderRoutines();
  }
}

async function runRoutineNow(routineId) {
  state.routines.inFlight = true;
  renderRoutines();
  setRoutineStatus("Running once within the reserved token budget...", "neutral");
  try {
    const payload = await routineApi(`/api/routines/${encodeURIComponent(routineId)}/run`, { method: "POST" });
    setRoutineStatus(payload.message || "Routine completed.", "success");
    await loadRoutines();
  } catch (error) {
    setRoutineStatus(error.message, "error");
    await loadRoutines();
  } finally {
    state.routines.inFlight = false;
    renderRoutines();
  }
}

async function toggleRoutine(routine) {
  state.routines.inFlight = true;
  renderRoutines();
  try {
    const payload = await routineApi(`/api/routines/${encodeURIComponent(routine.routine_id)}/enabled`, {
      method: "POST",
      body: JSON.stringify({ enabled: !routine.enabled }),
    });
    setRoutineStatus(payload.message, "success");
    await loadRoutines();
  } catch (error) {
    setRoutineStatus(error.message, "error");
  } finally {
    state.routines.inFlight = false;
    renderRoutines();
  }
}

async function deleteRoutine(routine) {
  if (!window.confirm(`Delete routine "${routine.name}" and all of its stored results?`)) {
    return;
  }
  state.routines.inFlight = true;
  renderRoutines();
  try {
    const payload = await routineApi(`/api/routines/${encodeURIComponent(routine.routine_id)}`, { method: "DELETE" });
    setRoutineStatus(payload.message, "success");
    await loadRoutines();
  } catch (error) {
    setRoutineStatus(error.message, "error");
  } finally {
    state.routines.inFlight = false;
    renderRoutines();
  }
}

async function deleteRoutineRunOutput(runId) {
  if (!window.confirm("Delete this routine output and any generated file? This cannot be undone.")) {
    return;
  }
  state.routines.inFlight = true;
  renderRoutines();
  try {
    const payload = await routineApi(`/api/routines/runs/${encodeURIComponent(runId)}`, { method: "DELETE" });
    setRoutineStatus(payload.message, "success");
    await loadRoutines();
  } catch (error) {
    setRoutineStatus(error.message, "error");
  } finally {
    state.routines.inFlight = false;
    renderRoutines();
  }
}

async function toggleRoutineSystemPause() {
  state.routines.inFlight = true;
  renderRoutines();
  try {
    const payload = await routineApi("/api/routines/admin/pause", {
      method: "POST",
      body: JSON.stringify({ paused: !state.routines.systemPaused }),
    });
    state.routines.systemPaused = Boolean(payload.system_paused);
    setRoutineStatus(payload.message, "success");
  } catch (error) {
    setRoutineStatus(error.message, "error");
  } finally {
    state.routines.inFlight = false;
    renderRoutines();
  }
}

// Conversation normalization and rendering ---------------------------------
// Normalization supports both current snake_case API data and older saved shapes.
function normalizeConversationMessage(message) {
  const role = message && typeof message.role === "string" ? message.role : "assistant";
  const rawBody = typeof message.body === "string" ? message.body : "";
  const legacyGeneratedDocument =
    message && message.generatedDocument
      ? { ...message.generatedDocument }
      : message && message.generated_document
        ? { ...message.generated_document }
        : null;
  const generatedDocuments = Array.isArray(message && message.generatedDocuments)
    ? message.generatedDocuments.map((document) => ({ ...document }))
    : Array.isArray(message && message.generated_documents)
      ? message.generated_documents.map((document) => ({ ...document }))
      : legacyGeneratedDocument
        ? [legacyGeneratedDocument]
        : [];
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
    body: role === "assistant" ? stripGeneratedUploadCitations(rawBody) : rawBody,
    images: Array.isArray(message && message.images)
      ? message.images
          .filter(
            (image) =>
              image &&
              CHAT_IMAGE_MIME_TYPES.has(image.mime_type) &&
              typeof image.content_base64 === "string" &&
              image.content_base64
          )
          .slice(0, CHAT_IMAGE_MAX_COUNT)
          .map((image) => ({
            filename: String(image.filename || "Attached image"),
            mime_type: image.mime_type,
            content_base64: image.content_base64,
          }))
      : [],
    citations: Array.isArray(message && message.citations)
      ? message.citations.map((item) => ({ ...item }))
      : [],
    toolTrace: Array.isArray(message && message.toolTrace)
      ? message.toolTrace.map((item) => ({ ...item }))
      : Array.isArray(message && message.tool_trace)
        ? message.tool_trace.map((item) => ({ ...item }))
        : [],
    generatedDocument: generatedDocuments[0] || legacyGeneratedDocument,
    generatedDocuments,
  };
}

function renderChatImagePreviews() {
  chatImagePreviewList.innerHTML = "";
  chatImagePreviewList.classList.toggle("is-hidden", state.chatImages.length === 0);
  state.chatImages.forEach((image) => {
    const preview = document.createElement("div");
    preview.className = "chat-image-preview";

    const imageNode = document.createElement("img");
    imageNode.src = `data:${image.mime_type};base64,${image.content_base64}`;
    imageNode.alt = image.filename;
    preview.appendChild(imageNode);

    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "chat-image-remove";
    removeButton.setAttribute("aria-label", `Remove ${image.filename}`);
    removeButton.title = `Remove ${image.filename}`;
    removeButton.textContent = "×";
    removeButton.disabled = state.sending;
    removeButton.addEventListener("click", () => {
      state.chatImages = state.chatImages.filter((item) => item.id !== image.id);
      renderChatImagePreviews();
      messageInput.focus();
    });
    preview.appendChild(removeButton);
    chatImagePreviewList.appendChild(preview);
  });
}

function readChatImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      const dataUrl = String(reader.result || "");
      const separatorIndex = dataUrl.indexOf(",");
      if (separatorIndex < 0) {
        reject(new Error(`Could not read ${file.name}.`));
        return;
      }
      resolve({
        id: crypto.randomUUID(),
        filename: file.name || "Attached image",
        mime_type: file.type,
        content_base64: dataUrl.slice(separatorIndex + 1),
      });
    });
    reader.addEventListener("error", () => {
      reject(new Error(`Could not read ${file.name}.`));
    });
    reader.readAsDataURL(file);
  });
}

async function addChatImageFiles(files) {
  if (!state.auth.user || state.auth.user.must_change_password) {
    openUserManagement(userLoginBadge);
    return;
  }
  const candidates = Array.from(files || []);
  if (!candidates.length) {
    return;
  }

  const remainingSlots = CHAT_IMAGE_MAX_COUNT - state.chatImages.length;
  if (remainingSlots <= 0) {
    setConversationMemoryStatus(
      `Attach up to ${CHAT_IMAGE_MAX_COUNT} images per message.`,
      "error"
    );
    return;
  }

  const accepted = [];
  const rejected = [];
  candidates.slice(0, remainingSlots).forEach((file) => {
    if (!CHAT_IMAGE_MIME_TYPES.has(file.type)) {
      rejected.push(`${file.name}: unsupported image type`);
    } else if (file.size > CHAT_IMAGE_MAX_BYTES) {
      rejected.push(`${file.name}: larger than 8 MB`);
    } else {
      accepted.push(file);
    }
  });
  if (candidates.length > remainingSlots) {
    rejected.push(`Only ${CHAT_IMAGE_MAX_COUNT} images can be attached`);
  }

  try {
    const images = await Promise.all(accepted.map((file) => readChatImage(file)));
    state.chatImages.push(...images);
    renderChatImagePreviews();
    if (rejected.length) {
      setConversationMemoryStatus(rejected.join(". "), "error");
    } else {
      setConversationMemoryStatus(
        `${state.chatImages.length} image${state.chatImages.length === 1 ? "" : "s"} ready to send.`,
        "success"
      );
    }
  } catch (error) {
    setConversationMemoryStatus(error.message, "error");
  }
}

function clearChatImages() {
  state.chatImages = [];
  chatImageInput.value = "";
  renderChatImagePreviews();
}

function closeMessageActionMenu() {
  const menuState = state.memory.messageActionMenu;
  if (menuState.button) {
    menuState.button.setAttribute("aria-expanded", "false");
  }
  if (menuState.node) {
    menuState.node.remove();
  }
  state.memory.messageActionMenu = {
    open: false,
    messageIndex: null,
    node: null,
    button: null,
  };
}

function openMessageActionMenu({ messageIndex, assistantMessageIndex, button, messageNode }) {
  closeMessageActionMenu();

  const menu = document.createElement("div");
  menu.className = "message-actions-menu";
  menu.setAttribute("role", "menu");

  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "message-actions-menu-item message-actions-menu-item-danger";
  deleteButton.textContent = "Delete chat entry";
  deleteButton.setAttribute("role", "menuitem");
  deleteButton.addEventListener("click", async (event) => {
    event.stopPropagation();
    closeMessageActionMenu();
    await deleteSavedConversationPair(assistantMessageIndex);
  });
  menu.appendChild(deleteButton);
  messageNode.appendChild(menu);
  deleteButton.focus();

  button.setAttribute("aria-expanded", "true");
  state.memory.messageActionMenu = {
    open: true,
    messageIndex,
    node: menu,
    button,
  };
}

function hasConversationMessages() {
  return state.messages.length > 0;
}

function getSavedConversationSummary(conversationId) {
  return state.memory.conversations.find((item) => item.conversation_id === conversationId) || null;
}

function sortSavedConversations(conversations) {
  return [...conversations].sort((left, right) => {
    const leftLabel = getSavedConversationDisplayLabel(left);
    const rightLabel = getSavedConversationDisplayLabel(right);
    const alphabeticalComparison = leftLabel.localeCompare(rightLabel, undefined, {
      sensitivity: "base",
      numeric: true,
    });
    if (alphabeticalComparison !== 0) {
      return alphabeticalComparison;
    }

    const exactComparison = leftLabel.localeCompare(rightLabel, undefined, {
      sensitivity: "variant",
      numeric: true,
    });
    if (exactComparison !== 0) {
      return exactComparison;
    }

    return String(left.conversation_id || "").localeCompare(
      String(right.conversation_id || "")
    );
  });
}

function getMostRecentlyUpdatedSavedConversation(conversations) {
  return conversations.reduce((mostRecent, candidate) => {
    if (!mostRecent) {
      return candidate;
    }
    const candidateUpdatedAt = Date.parse(candidate.updated_at || "") || 0;
    const mostRecentUpdatedAt = Date.parse(mostRecent.updated_at || "") || 0;
    if (candidateUpdatedAt !== mostRecentUpdatedAt) {
      return candidateUpdatedAt > mostRecentUpdatedAt ? candidate : mostRecent;
    }
    const candidateCreatedAt = Date.parse(candidate.created_at || "") || 0;
    const mostRecentCreatedAt = Date.parse(mostRecent.created_at || "") || 0;
    if (candidateCreatedAt !== mostRecentCreatedAt) {
      return candidateCreatedAt > mostRecentCreatedAt ? candidate : mostRecent;
    }
    return String(candidate.conversation_id || "").localeCompare(
      String(mostRecent.conversation_id || "")
    ) > 0
      ? candidate
      : mostRecent;
  }, null);
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
    reasoning_mode: conversation.reasoning_mode,
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

// Safe, deliberately small Markdown renderer -------------------------------
// Escape raw HTML first, then introduce only the markup this UI supports.
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function isSafeImageUrl(value) {
  const normalized = String(value || "").trim();
  return (
    /^https?:\/\//i.test(normalized) ||
    /^data:image\/(?:jpeg|png|webp|gif);base64,[a-z0-9+/=\s]+$/i.test(normalized)
  );
}

function isDirectImageUrl(value) {
  const normalized = String(value || "").trim();
  if (!isSafeImageUrl(normalized)) {
    return false;
  }

  try {
    const parsed = new URL(normalized);
    return /\.(?:avif|gif|jpe?g|png|svg|webp)$/i.test(parsed.pathname);
  } catch {
    return false;
  }
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
    /!\[([^\]\n]*)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, altText, imageUrl) => {
      const normalizedAlt = altText || "Referenced image";
      const caption = altText
        ? `<span class="rendered-markdown-image-caption">${altText}</span>`
        : "";
      return stash(
        `<span class="rendered-markdown-image-wrap">` +
          `<a class="rendered-markdown-image-link" href="${imageUrl}" ` +
          `data-image-alt="${normalizedAlt}" target="_blank" rel="noreferrer">` +
          `<img class="rendered-markdown-image" src="${imageUrl}" alt="${normalizedAlt}" ` +
          `loading="lazy" decoding="async">` +
          `</a>${caption}</span>`
      );
    }
  );

  rendered = rendered.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, label, url) => stash(`<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`)
  );

  rendered = rendered.replace(/\*\*([\s\S]+?)\*\*/g, "<strong>$1</strong>");
  rendered = rendered.replace(/__([\s\S]+?)__/g, "<strong>$1</strong>");
  rendered = rendered.replace(/~~([\s\S]+?)~~/g, "<del>$1</del>");

  return rendered.replace(/@@MD(\d+)@@/g, (_, index) => placeholders[Number(index)] || "");
}

function splitMarkdownTableRow(line) {
  let normalized = String(line || "").trim();
  if (!normalized.includes("|")) {
    return null;
  }
  if (normalized.startsWith("|")) {
    normalized = normalized.slice(1);
  }
  if (normalized.endsWith("|") && !normalized.endsWith("\\|")) {
    normalized = normalized.slice(0, -1);
  }

  const cells = [];
  let cell = "";
  let escaped = false;
  for (const character of normalized) {
    if (escaped) {
      cell += character;
      escaped = false;
      continue;
    }
    if (character === "\\") {
      escaped = true;
      cell += character;
      continue;
    }
    if (character === "|") {
      cells.push(cell.trim().replace(/\\\|/g, "|"));
      cell = "";
      continue;
    }
    cell += character;
  }
  if (escaped) {
    cell += "\\";
  }
  cells.push(cell.trim().replace(/\\\|/g, "|"));
  return cells.length >= 2 ? cells : null;
}

function parseMarkdownTable(lines, startIndex) {
  const headerCells = splitMarkdownTableRow(lines[startIndex]);
  const dividerCells = splitMarkdownTableRow(lines[startIndex + 1]);
  if (
    !headerCells ||
    !dividerCells ||
    headerCells.length !== dividerCells.length ||
    !dividerCells.every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s/g, "")))
  ) {
    return null;
  }

  const alignments = dividerCells.map((cell) => {
    const normalized = cell.replace(/\s/g, "");
    if (normalized.startsWith(":") && normalized.endsWith(":")) {
      return "center";
    }
    return normalized.endsWith(":") ? "right" : "left";
  });
  const rows = [];
  let nextIndex = startIndex + 2;
  while (nextIndex < lines.length) {
    const cells = splitMarkdownTableRow(lines[nextIndex]);
    if (!cells || !lines[nextIndex].trim()) {
      break;
    }
    rows.push(headerCells.map((_, columnIndex) => cells[columnIndex] || ""));
    nextIndex += 1;
  }

  const header = headerCells
    .map(
      (cell, index) =>
        `<th class="markdown-table-align-${alignments[index]}">${renderInlineMarkdown(cell)}</th>`
    )
    .join("");
  const body = rows
    .map(
      (row) =>
        `<tr>${row
          .map(
            (cell, index) =>
              `<td class="markdown-table-align-${alignments[index]}">${renderInlineMarkdown(cell)}</td>`
          )
          .join("")}</tr>`
    )
    .join("");
  return {
    html:
      `<div class="markdown-table-scroll" role="region" aria-label="Response table" tabindex="0">` +
      `<table class="markdown-table"><thead><tr>${header}</tr></thead>` +
      `<tbody>${body}</tbody></table></div>`,
    endIndex: nextIndex - 1,
  };
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
  let skipThroughIndex = -1;

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

  lines.forEach((line, lineIndex) => {
    if (lineIndex <= skipThroughIndex) {
      return;
    }
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

    const table = parseMarkdownTable(lines, lineIndex);
    if (table) {
      flushTextBlocks();
      blocks.push(table.html);
      skipThroughIndex = table.endIndex;
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

function openImageLightbox(imageUrl, caption = "", trigger = null) {
  const normalizedUrl = String(imageUrl || "").trim();
  if (!isSafeImageUrl(normalizedUrl)) {
    return;
  }

  imageLightboxReturnFocus = trigger instanceof HTMLElement ? trigger : document.activeElement;
  imageLightboxImage.src = normalizedUrl;
  imageLightboxImage.alt = String(caption || "Expanded image");
  imageLightboxCaption.textContent = String(caption || "");
  imageLightboxCaption.classList.toggle("is-hidden", !caption);
  imageLightbox.classList.remove("is-hidden");
  imageLightbox.setAttribute("aria-hidden", "false");
  document.body.classList.add("is-image-lightbox-open");
  imageLightboxCloseButton.focus();
}

function closeImageLightbox() {
  if (imageLightbox.classList.contains("is-hidden")) {
    return;
  }

  imageLightbox.classList.add("is-hidden");
  imageLightbox.setAttribute("aria-hidden", "true");
  document.body.classList.remove("is-image-lightbox-open");
  imageLightboxImage.removeAttribute("src");
  imageLightboxImage.alt = "";
  imageLightboxCaption.textContent = "";
  if (imageLightboxReturnFocus instanceof HTMLElement && imageLightboxReturnFocus.isConnected) {
    imageLightboxReturnFocus.focus();
  }
  imageLightboxReturnFocus = null;
}

function renderMessage(message, options = {}) {
  const normalized = normalizeConversationMessage(message);
  let messageIndex = null;
  if (options.persist !== false) {
    state.messages.push(normalized);
    messageIndex = state.messages.length - 1;
  }

  const node = messageTemplate.content.firstElementChild.cloneNode(true);
  node.classList.add(normalized.role);
  node.querySelector(".message-meta").textContent = normalized.label;
  const messageBody = node.querySelector(".message-body");
  messageBody.innerHTML = renderMarkdown(normalized.body);
  if (normalized.images.length > 0) {
    const imageList = document.createElement("div");
    imageList.className = "message-image-list";
    normalized.images.forEach((image) => {
      const imageUrl = `data:${image.mime_type};base64,${image.content_base64}`;
      const imageButton = document.createElement("button");
      imageButton.type = "button";
      imageButton.className = "message-image-button";
      imageButton.title = `Expand ${image.filename}`;
      const imageNode = document.createElement("img");
      imageNode.src = imageUrl;
      imageNode.alt = image.filename;
      imageNode.loading = "lazy";
      imageNode.decoding = "async";
      imageButton.appendChild(imageNode);
      imageButton.addEventListener("click", () => {
        openImageLightbox(imageUrl, image.filename, imageButton);
      });
      imageList.appendChild(imageButton);
    });
    messageBody.prepend(imageList);
  }

  const footer = node.querySelector(".message-footer");

  if (normalized.generatedDocuments.length > 0) {
    const generatedDocumentSection = document.createElement("div");
    generatedDocumentSection.className = "generated-document-section";

    const generatedDocumentLabel = document.createElement("div");
    generatedDocumentLabel.className = "generated-document-title";
    const sourceDocumentCount = normalized.generatedDocuments.filter(
      (document) => document.document_kind === "source"
    ).length;
    generatedDocumentLabel.textContent =
      sourceDocumentCount === normalized.generatedDocuments.length
        ? sourceDocumentCount === 1
          ? "Original source document:"
          : "Original source documents:"
        : normalized.generatedDocuments.length === 1
          ? "Generated file:"
          : "Generated files:";
    generatedDocumentSection.appendChild(generatedDocumentLabel);

    normalized.generatedDocuments.forEach((generatedFile) => {
      const downloadButton = document.createElement("button");
      downloadButton.type = "button";
      downloadButton.className = "generated-document-button";
      downloadButton.textContent = generatedFile.title
        ? `${generatedFile.title} (${generatedFile.filename})`
        : generatedFile.filename;
      downloadButton.addEventListener("click", () => {
        downloadBase64File(
          generatedFile.filename,
          generatedFile.mime_type,
          generatedFile.content_base64
        );
      });
      generatedDocumentSection.appendChild(downloadButton);
    });
    if (sourceDocumentCount > 0) {
      const sourceNote = document.createElement("div");
      sourceNote.className = "generated-document-title";
      sourceNote.textContent =
        "Attached to this private conversation; not added to the library.";
      generatedDocumentSection.appendChild(sourceNote);
    }
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
      const citationImageUrl = isDirectImageUrl(citation.source_url)
        ? citation.source_url
        : null;
      if (citationImageUrl) {
        const imageLabel = citation.title || "Cited image";
        const target = document.createElement("button");
        target.type = "button";
        target.className = "citation citation-button citation-image-button";
        target.title = `Expand cited image: ${imageLabel}`;

        const thumbnail = document.createElement("img");
        thumbnail.className = "citation-image-thumbnail";
        thumbnail.src = citationImageUrl;
        thumbnail.alt = imageLabel;
        thumbnail.loading = "lazy";
        thumbnail.decoding = "async";
        target.appendChild(thumbnail);

        const label = document.createElement("span");
        label.className = "citation-image-label";
        label.textContent = imageLabel;
        target.appendChild(label);
        target.addEventListener("click", () => {
          openImageLightbox(citationImageUrl, imageLabel, target);
        });
        citationList.appendChild(target);
        return;
      }

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
      const displayLabel = getDocumentDisplayLabel(citation);
      target.textContent = displayLabel;
      target.title = `Open ${displayLabel}`;
      target.addEventListener("click", async () => {
        await openCitationDocument(citation);
      });
      citationList.appendChild(target);
    });

    footer.appendChild(citationList);
  }

  const savedConversation = getSavedConversationSummary(state.conversationId);
  const isSavedConversation = Boolean(savedConversation);
  const isPersistedMessage =
    isSavedConversation &&
    messageIndex !== null &&
    messageIndex < Number(savedConversation.message_count || 0);
  let pairAssistantMessageIndex = null;
  if (isPersistedMessage && normalized.role === "assistant") {
    if (messageIndex > 0 && state.messages[messageIndex - 1]?.role === "user") {
      pairAssistantMessageIndex = messageIndex;
    }
  } else if (isPersistedMessage && normalized.role === "user") {
    if (state.messages[messageIndex + 1]?.role === "assistant") {
      pairAssistantMessageIndex = messageIndex + 1;
    }
  }
  if (pairAssistantMessageIndex !== null) {
    node.classList.add("has-message-actions");
    const messageActionsButton = document.createElement("button");
    messageActionsButton.type = "button";
    messageActionsButton.className = "message-actions-button";
    messageActionsButton.textContent = "⋯";
    messageActionsButton.setAttribute(
      "aria-label",
      "More chat entry actions"
    );
    messageActionsButton.setAttribute("aria-haspopup", "menu");
    messageActionsButton.setAttribute("aria-expanded", "false");
    messageActionsButton.title = "More chat entry actions";
    messageActionsButton.disabled =
      state.memory.pairDeleteInFlightIndex !== null ||
      state.sending ||
      state.memory.saveInFlight;
    messageActionsButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const menuState = state.memory.messageActionMenu;
      if (menuState.open && menuState.messageIndex === messageIndex) {
        closeMessageActionMenu();
        return;
      }
      openMessageActionMenu({
        messageIndex,
        assistantMessageIndex: pairAssistantMessageIndex,
        button: messageActionsButton,
        messageNode: node,
      });
    });
    node.insertBefore(messageActionsButton, node.firstChild);
  }

  messageList.appendChild(node);
  if (options.scrollToLatest !== false) {
    messageList.scrollTop = messageList.scrollHeight;
  }
  renderConversationSaveButton();
}

// In-flight request and progress indicators --------------------------------
function hideResponsePreparationIndicator() {
  if (state.responseIndicatorTimer !== null) {
    window.clearInterval(state.responseIndicatorTimer);
    state.responseIndicatorTimer = null;
  }
  if (!state.responseIndicatorNode) {
    return;
  }
  state.responseIndicatorNode.remove();
  state.responseIndicatorNode = null;
}

function showResponsePreparationIndicator(statusText = "Preparing response") {
  hideResponsePreparationIndicator();

  const node = messageTemplate.content.firstElementChild.cloneNode(true);
  node.classList.add("assistant", "preparing");
  node.querySelector(".message-meta").textContent = "Assistant";
  node.querySelector(".message-body").innerHTML = `
    <div class="response-preparing" role="status" aria-live="polite" aria-label="Assistant is preparing a response">
      <span class="response-preparing-text"></span>
      <span class="response-preparing-dots" aria-hidden="true">
        <span></span>
        <span></span>
        <span></span>
      </span>
    </div>
  `;
  node.querySelector(".message-footer").remove();
  const statusNode = node.querySelector(".response-preparing-text");
  const startedAt = Date.now();
  const updateStatus = () => {
    const elapsedSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    statusNode.textContent =
      elapsedSeconds > 0 ? `${statusText} · ${elapsedSeconds}s` : statusText;
  };
  updateStatus();
  state.responseIndicatorTimer = window.setInterval(updateStatus, 1000);

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
  const authenticated =
    Boolean(state.auth.user) && !state.auth.user.must_change_password;
  const composerBusy =
    !authenticated ||
    isSending ||
    state.memory.pairDeleteInFlightIndex !== null;
  sendButton.disabled = composerBusy;
  messageInput.disabled = composerBusy;
  addChatImagesButton.disabled = composerBusy;
  chatImageInput.disabled = composerBusy;
  sourceModeButton.disabled = composerBusy;
  reasoningModeButton.disabled = composerBusy;
  sendButton.textContent = isSending ? "Sending..." : "Send";
  cancelResponseButton.classList.toggle("is-hidden", !isSending);
  cancelResponseButton.disabled = !isSending;
  if (!isSending) {
    cancelResponseButton.textContent = "Cancel response";
  }
  renderChatImagePreviews();
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
    openSourceLocationButton,
  ].forEach((element) => {
    if (element) {
      element.disabled = isInFlight || !canManageLibrary();
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

// Watched-folder synchronization UI ----------------------------------------
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
      !canManageLibrary() ||
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
  if (!canManageLibrary()) {
    state.library.watchFolders = [];
    state.library.watchFoldersLoaded = true;
    state.library.watchFoldersLoadError = null;
    renderWatchFolderList();
    return;
  }
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

async function openWatchedFolderSource(watchId) {
  if (
    !watchId ||
    state.library.watchFolderInFlight ||
    state.library.openSourceLocationInFlight
  ) {
    return;
  }

  state.library.openSourceLocationInFlight = true;
  renderDocumentEditor();
  setLibraryActionStatus("Opening the synchronized source location...");

  try {
    const response = await fetch(
      `/api/watch-folders/${encodeURIComponent(watchId)}/open-source`,
      { method: "POST" }
    );
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail
        ? payload.detail
        : "Open source location failed";
      throw new Error(detail);
    }
    setLibraryActionStatus(
      payload.message || "Opened the synchronized source location.",
      "success"
    );
  } catch (error) {
    setLibraryActionStatus(`Open source location failed: ${error.message}`, "error");
  } finally {
    state.library.openSourceLocationInFlight = false;
    renderDocumentEditor();
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

// Document generation and library mutation controls ------------------------
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
  if (isDeleting) {
    state.library.deleteProgress = {
      phase: "starting",
      percent: 0,
      detail: "Starting deletion...",
      startedAt: Date.now(),
    };
  } else {
    state.library.deleteProgress = null;
  }
  renderDeleteSelectionSummary();
  renderDeleteProgressStatus();
  renderFolderTree();
  renderDocumentFileList();
}

function updateDeleteProgress(event) {
  if (!state.library.deleteInFlight) {
    return;
  }
  const existing = state.library.deleteProgress || { startedAt: Date.now() };
  state.library.deleteProgress = {
    phase: String(event.phase || existing.phase || "deleting"),
    percent: Math.max(0, Math.min(100, Number(event.percent) || 0)),
    detail: String(event.detail || existing.detail || "Updating the library..."),
    startedAt: existing.startedAt || Date.now(),
  };
  renderDeleteSelectionSummary();
  renderDeleteProgressStatus();
}

function renderDeleteProgressStatus() {
  const progress = state.library.deleteProgress;
  if (!state.library.deleteInFlight || !progress) {
    deleteProgressPanel?.classList.add("is-hidden");
    if (deleteProgressBar) {
      deleteProgressBar.style.width = "0%";
    }
    if (deleteProgressTrack) {
      deleteProgressTrack.setAttribute("aria-valuenow", "0");
    }
    return;
  }
  deleteProgressPanel?.classList.remove("is-hidden");
  const elapsedSeconds = Math.max(
    0,
    Math.floor((Date.now() - progress.startedAt) / 1000)
  );
  const percent = Math.round(progress.percent);
  if (deleteProgressBar) {
    deleteProgressBar.style.width = `${percent}%`;
  }
  if (deleteProgressTrack) {
    deleteProgressTrack.setAttribute("aria-valuenow", String(percent));
  }
  if (deleteProgressLabel) {
    deleteProgressLabel.textContent = `${progress.detail} ${percent}% · ${elapsedSeconds}s`;
  }
  setLibraryActionStatus(
    `${progress.detail} ${percent}% · ${elapsedSeconds}s`
  );
}

async function requestDocumentDeletion(documentIds) {
  const response = await fetch("/api/documents/delete/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      document_ids: documentIds,
    }),
  });
  if (!response.ok) {
    const payload = await parseJsonResponse(response);
    throw new Error(payload?.detail || "Delete failed");
  }

  let resultPayload = null;
  let bufferedText = "";
  const handleLine = (line) => {
    const normalized = String(line || "").trim();
    if (!normalized) {
      return;
    }
    let event;
    try {
      event = JSON.parse(normalized);
    } catch (_error) {
      throw new Error("The deletion progress stream returned invalid data.");
    }
    if (event.type === "progress") {
      updateDeleteProgress(event);
      return;
    }
    if (event.type === "error") {
      throw new Error(event.detail || "Delete failed");
    }
    if (event.type === "result") {
      resultPayload = event.payload || null;
    }
  };

  const elapsedTimer = window.setInterval(renderDeleteProgressStatus, 1000);
  try {
    if (!response.body || typeof response.body.getReader !== "function") {
      bufferedText = await response.text();
      bufferedText.split(/\r?\n/).forEach(handleLine);
    } else {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const { value, done } = await reader.read();
        bufferedText += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = bufferedText.split(/\r?\n/);
        bufferedText = done ? "" : lines.pop() || "";
        lines.forEach(handleLine);
        if (done) {
          if (bufferedText.trim()) {
            handleLine(bufferedText);
          }
          break;
        }
      }
    }
  } finally {
    window.clearInterval(elapsedTimer);
  }
  if (!resultPayload) {
    throw new Error("The deletion completed without a result.");
  }
  return resultPayload;
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
  documentEditorTagChips.querySelectorAll(".tag-chip-remove").forEach((button) => {
    button.disabled = isUpdating || !canEdit;
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
    if (state.sourceMode === "broader") {
      return "No internal document scope is selected. Global context is active.";
    }
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
      ? state.sourceMode === "broader"
        ? "No internal document scope selected. Global context is active."
        : "All indexed documents are currently available for retrieval."
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
    if (state.sourceMode === "broader") {
      return `${audienceLabel}: global context is active; no internal documents are selected.`;
    }
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
  const routineScopeMode = state.library.scopeSelectionTarget === "routine";
  scopeInventorySummary.textContent =
    routineScopeMode
      ? "Routine scope is independent from the current conversation scope."
      : state.sourceMode === "broader" &&
        state.library.draftContext.folderIds.length === 0 &&
        state.library.draftContext.documentIds.length === 0
      ? "Global context is active; no internal documents are selected."
      : draftCoverage.excludedDocuments.length === 0
        ? "Everything in the indexed library is currently in scope."
        : `${draftCoverage.includedDocuments.length} document${draftCoverage.includedDocuments.length === 1 ? "" : "s"} in scope, ${draftCoverage.excludedDocuments.length} outside scope.`;
  scopeAppliedSummary.textContent = routineScopeMode
    ? "The current chat scope will not be changed by these selections."
    : buildScopeStatusText(state.library.appliedContext, appliedCoverage, "Applied");
  scopeDraftSummary.textContent = buildScopeStatusText(
    state.library.draftContext,
    draftCoverage,
    routineScopeMode ? "Routine selected" : "Selected"
  );

  scopeIncludedList.innerHTML = "";
  if (
    state.library.draftContext.folderIds.length === 0 &&
    state.library.draftContext.documentIds.length === 0
  ) {
    const empty = document.createElement("p");
    empty.className = "scope-list-empty";
    empty.textContent =
      routineScopeMode
        ? "No routine documents are selected yet."
        : state.sourceMode === "broader"
        ? "No internal documents are selected while Global context is active."
        : "All indexed documents are included right now.";
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
    empty.textContent = "Nothing is excluded from the current scope.";
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
  deleteSelectedButton.textContent = state.library.deleteInFlight
    ? `Deleting ${Math.round(state.library.deleteProgress?.percent || 0)}%`
    : "Delete selected";
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

// Saved-conversation memory -------------------------------------------------
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
    !state.auth.user ||
    state.auth.user.must_change_password ||
    state.memory.saveInFlight ||
    Boolean(state.memory.renameInFlightId) ||
    state.memory.pairDeleteInFlightIndex !== null ||
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

  if (!state.auth.user || state.auth.user.must_change_password) {
    const signedOut = document.createElement("p");
    signedOut.className = "saved-conversation-empty";
    signedOut.textContent = state.auth.user
      ? "Change your temporary password to access your chats."
      : "Sign in to see your chats.";
    savedConversationList.appendChild(signedOut);
    return;
  }

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
  if (!state.auth.user || state.auth.user.must_change_password) {
    setConversationMemoryStatus(
      state.auth.user
        ? "Change your temporary password to access conversations."
        : "Sign in to access your private conversations."
    );
    return;
  }

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
  state.sourceMode = sourceMode === "broader" ? "broader" : "internal";
  const isBroader = state.sourceMode === "broader";
  sourceModeButton.classList.toggle("is-broader", isBroader);
  sourceModeButton.setAttribute("aria-pressed", String(isBroader));
  sourceModeButton.setAttribute(
    "aria-label",
    isBroader ? "Context: Global" : "Context: Internal"
  );
  sourceModeButton.title = isBroader
    ? "Context: Global is active. Click for Context: Internal."
    : "Context: Internal is active. Click for Context: Global.";
  sourceModeLabel.textContent = isBroader ? "Context: Global" : "Context: Internal";
  composerNote.textContent = modeCopy[state.sourceMode].composerNote;
}

function applyReasoningMode(reasoningMode) {
  state.reasoningMode = reasoningMode === "maximum" ? "maximum" : "standard";
  const isMaximum = state.reasoningMode === "maximum";
  reasoningModeButton.classList.toggle("is-maximum", isMaximum);
  reasoningModeButton.setAttribute("aria-pressed", String(isMaximum));
  reasoningModeButton.setAttribute(
    "aria-label",
    isMaximum ? "Maximum reasoning" : "Standard reasoning"
  );
  reasoningModeButton.title = isMaximum
    ? "Maximum reasoning uses GPT-5.6 Terra. Click for Standard reasoning."
    : "Standard reasoning uses GPT-5.6 Luna. Click for Maximum reasoning.";
  reasoningModeLabel.textContent = isMaximum ? "Maximum reasoning" : "Standard reasoning";
}

function applyConversationPreferences(preferences) {
  const normalized = normalizeConversationPreferences(preferences);
  applySourceMode(normalized.sourceMode);
  applyReasoningMode(normalized.reasoningMode);
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
  closeMessageActionMenu();
  state.messages = [];
  state.responseIndicatorNode = null;
  messageList.innerHTML = "";

  if (!Array.isArray(messages) || messages.length === 0) {
    renderIntroMessage();
    restoreConversationScrollPosition(state.conversationId);
    return;
  }

  messages.forEach((message) => {
    renderMessage(message, { scrollToLatest: false });
  });
  restoreConversationScrollPosition(state.conversationId);
}

function resetConversation(options = {}) {
  const { rememberCurrentScroll = true } = options;
  if (rememberCurrentScroll) {
    rememberActiveConversationScrollPosition();
  }
  state.conversationId = crypto.randomUUID();
  clearChatImages();
  applyConversationPreferences(getDefaultConversationPreferences());
  rememberConversationPreferences(state.conversationId);
  renderConversationMessages([]);
  renderSavedConversationList();
  renderConversationSaveButton();
  updateConversationMemoryStatus();
}

async function loadSavedConversations(options = {}) {
  const { openMostRecent = false } = options;
  if (!state.auth.user || state.auth.user.must_change_password) {
    state.memory.conversations = [];
    state.memory.loaded = true;
    state.memory.loadError = null;
    renderSavedConversationList();
    renderConversationSaveButton();
    updateConversationMemoryStatus();
    return;
  }
  const conversationIdWhenLoadingStarted = state.conversationId;
  let mostRecentConversation = null;
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
    mostRecentConversation = getMostRecentlyUpdatedSavedConversation(
      state.memory.conversations
    );
  } catch (error) {
    state.memory.conversations = [];
    state.memory.loaded = true;
    state.memory.loadError = error.message;
  }

  renderSavedConversationList();
  renderConversationSaveButton();
  updateConversationMemoryStatus();

  if (
    openMostRecent &&
    mostRecentConversation &&
    state.conversationId === conversationIdWhenLoadingStarted &&
    !state.sending &&
    !String(messageInput.value || "").trim()
  ) {
    await openSavedConversation(mostRecentConversation.conversation_id, {
      rememberCurrentScroll: false,
    });
  }
}

async function openSavedConversation(conversationId, options = {}) {
  const { rememberCurrentScroll = true } = options;
  if (!conversationId || state.sending) {
    return;
  }

  if (rememberCurrentScroll) {
    rememberActiveConversationScrollPosition();
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
        reasoning_mode: state.reasoningMode,
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
    renderConversationMessages(state.messages);
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

async function deleteSavedConversationPair(assistantMessageIndex) {
  const conversationId = state.conversationId;
  if (
    !conversationId ||
    !getSavedConversationSummary(conversationId) ||
    state.memory.pairDeleteInFlightIndex !== null ||
    !Number.isInteger(assistantMessageIndex)
  ) {
    return;
  }

  const userMessage = state.messages[assistantMessageIndex - 1];
  const assistantMessage = state.messages[assistantMessageIndex];
  if (
    !userMessage ||
    userMessage.role !== "user" ||
    !assistantMessage ||
    assistantMessage.role !== "assistant"
  ) {
    return;
  }

  const confirmed = window.confirm(
    "Delete this question and response from the saved conversation?"
  );
  if (!confirmed) {
    return;
  }

  const previousMessages = [...state.messages];
  state.memory.pairDeleteInFlightIndex = assistantMessageIndex;
  setComposerState(state.sending);
  renderConversationMessages(previousMessages);
  renderSavedConversationList();
  renderConversationSaveButton();
  setConversationMemoryStatus("Deleting question and response...");

  try {
    const response = await fetch(
      `/api/conversations/${encodeURIComponent(conversationId)}/pairs/${assistantMessageIndex}`,
      { method: "DELETE" }
    );
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Delete failed";
      throw new Error(detail);
    }
    if (!payload || !payload.conversation) {
      throw new Error("The server returned no updated conversation.");
    }

    upsertSavedConversationSummary(buildConversationSummary(payload.conversation));
    renderConversationMessages(payload.conversation.messages || []);
    renderSavedConversationList();
    setConversationMemoryStatus(
      "Question and response deleted from this conversation.",
      "success"
    );
  } catch (error) {
    renderConversationMessages(previousMessages);
    setConversationMemoryStatus(`Delete failed: ${error.message}`, "error");
  } finally {
    state.memory.pairDeleteInFlightIndex = null;
    setComposerState(state.sending);
    renderConversationMessages(state.messages);
    renderSavedConversationList();
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
      forgetConversationScrollPosition(conversationId);
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

// Library explorer rendering ------------------------------------------------
// These renderers derive counts, breadcrumbs, tree rows, and files from state.
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
    baseParts.push(state.sourceMode === "broader" ? "scope: global" : "scope: all docs");
  } else {
    baseParts.push(
      `scope: ${draft.folderIds.length} folder${draft.folderIds.length === 1 ? "" : "s"}, ` +
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
  if (state.library.scopeSelectionTarget === "routine") {
    state.routines.draftContext = cloneContextFilter(state.library.draftContext);
    renderScopePane();
    renderFolderTree();
    renderDocumentFileList();
    renderRoutines();
    return;
  }
  commitDraftContextScope();
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
  if (state.library.scopeSelectionTarget === "routine") {
    state.routines.draftContext = cloneContextFilter(state.library.draftContext);
    renderScopePane();
    renderFolderTree();
    renderDocumentFileList();
    renderRoutines();
    return;
  }
  commitDraftContextScope();
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
    const includedViaFolder = state.library.draftContext.folderIds.some((folderId) =>
      folderPathContainsFolder(folderId, docSummary.folder)
    );
    const isScopedDirectly = state.library.draftContext.documentIds.includes(docSummary.document_id);
    row.classList.toggle("is-scoped", includedViaFolder || isScopedDirectly);
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
    const isFolderScopedDirectly = Boolean(
      node.folder && state.library.draftContext.folderIds.includes(node.folder.folder_id)
    );
    const isFolderScopedViaAncestor = Boolean(
      node.folder &&
        !isFolderScopedDirectly &&
        state.library.draftContext.folderIds.some((folderId) =>
          folderPathContainsFolder(folderId, node.folder.folder_id)
        )
    );
    row.classList.toggle("is-scoped", isFolderScopedDirectly || isFolderScopedViaAncestor);
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
        setActiveFolder(node.pathId, { renderTree: false });
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
      action.classList.toggle("is-active", isFolderScopedDirectly || isFolderScopedViaAncestor);
      action.disabled = isFolderScopedViaAncestor;
      action.textContent = isFolderScopedDirectly
        ? "Scoped"
        : isFolderScopedViaAncestor
          ? "Parent"
          : "Scope";
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
    const includedViaFolder = state.library.draftContext.folderIds.some((folderId) =>
      folderPathContainsFolder(folderId, docSummary.folder)
    );
    const isScopedDirectly = state.library.draftContext.documentIds.includes(docSummary.document_id);
    row.classList.toggle("is-scoped", includedViaFolder || isScopedDirectly);
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
  renderPreview();
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
    state.library.previewPanelState = "open";
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

function closeDocumentPreview() {
  state.library.previewDocumentId = null;
  state.library.previewPanelState = "closed";
  state.library.editorDismissed = false;
  renderBrowserStats();
  renderLibraryExplorer();
  renderPreview();
}

function toggleDocumentPreviewMinimized() {
  if (!getPreviewDocument()) {
    return;
  }
  state.library.previewPanelState =
    state.library.previewPanelState === "minimized" ? "open" : "minimized";
  renderPreview();
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
    const payload = await requestDocumentDeletion(selectedIds);

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
    renderDocumentEditorTags();
    return;
  }

  documentEditorForm.dataset.documentId = previewDoc.document_id;
  documentEditorTitleInput.value = previewDoc.title || "";
  documentEditorCategoryInput.value = previewDoc.category || "";
  documentEditorFolderInput.value = normalizeFolderPath(previewDoc.folder);
  documentEditorTagsInput.value = stripAutoTagsForFolder(previewDoc.tags || [], previewDoc.folder).join(", ");
  renderDocumentEditorTags();
  state.library.editorDocumentId = previewDoc.document_id;
  state.library.editorDirty = false;
}

function renderFolderPropertyTags() {
  const folderId = state.library.activeFolderId;
  const watchedFolder = getWatchedFolderForLibraryFolder(folderId);
  folderPropertiesTags.innerHTML = "";

  if (!folderId) {
    return;
  }

  const manualTags = watchedFolder
    ? stripAutoTagsForFolder(folderPropertiesTagsInput.value || "", folderId)
    : [];
  const autoTags = buildFolderAutoTags(folderId);
  const tags = normalizeTagItems([...manualTags, ...autoTags]);
  if (tags.length === 0) {
    const empty = document.createElement("span");
    empty.className = "folder-property-empty";
    empty.textContent = "No tags";
    folderPropertiesTags.appendChild(empty);
    return;
  }

  const manualTagKeys = new Set(manualTags.map((tag) => tag.toLowerCase()));
  const autoTagKeys = new Set(autoTags.map((tag) => tag.toLowerCase()));
  const canRemove = Boolean(watchedFolder) && !state.library.watchFolderInFlight;
  tags.forEach((tag) => {
    const isAutoTag = autoTagKeys.has(tag.toLowerCase()) && !manualTagKeys.has(tag.toLowerCase());
    folderPropertiesTags.appendChild(createTagChip(tag, {
      className: `folder-property-tag${isAutoTag ? " folder-property-tag-auto" : ""}`,
      removable: canRemove && !isAutoTag,
      onRemove: removeFolderPropertyTag,
      title: isAutoTag ? "Automatically generated from the folder path" : "",
    }));
  });
}

function removeFolderPropertyTag(tagToRemove) {
  const nextTags = normalizeTagItems(folderPropertiesTagsInput.value || "")
    .filter((tag) => tag.toLowerCase() !== tagToRemove.toLowerCase());
  folderPropertiesTagsInput.value = nextTags.join(", ");
  renderFolderPropertyTags();
  setLibraryActionStatus(`Removed “${tagToRemove}”. Save settings to apply it.`);
  folderPropertiesTagsInput.focus();
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

  renderFolderPropertyTags();

  renameSelectedFolderButton.textContent = watchedFolder ? "Save settings" : "Rename folder";
  const folderActionInFlight =
    state.library.watchFolderInFlight || state.library.openSourceLocationInFlight;
  renameSelectedFolderButton.disabled = watchedFolder
    ? folderActionInFlight
    : !canMutateLibrary();
  folderPropertiesAliasInput.disabled = !watchedFolder || folderActionInFlight;
  openSourceLocationButton.classList.toggle("is-hidden", !watchedFolder);
  openSourceLocationButton.disabled = !watchedFolder || folderActionInFlight;
  openSourceLocationButton.textContent = state.library.openSourceLocationInFlight
    ? "Opening..."
    : "Open source location";
  syncSelectedFolderButton.classList.toggle("is-hidden", !watchedFolder);
  syncSelectedFolderButton.disabled = !watchedFolder || folderActionInFlight;
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
    const payload = await requestDocumentDeletion([documentId]);

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

// Document preview and metadata editor -------------------------------------
function renderPreviewSourceMedia(previewDoc) {
  previewSourceMedia.innerHTML = "";
  const imageUrl = previewDoc && isDirectImageUrl(previewDoc.source_url)
    ? previewDoc.source_url
    : null;
  previewSourceMedia.classList.toggle("is-hidden", !imageUrl);
  if (!imageUrl) {
    return;
  }

  const imageLabel = previewDoc.title || "Document source image";
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "preview-source-image-button";
  trigger.title = `Expand ${imageLabel}`;

  const image = document.createElement("img");
  image.className = "preview-source-image";
  image.src = imageUrl;
  image.alt = imageLabel;
  image.loading = "lazy";
  image.decoding = "async";
  trigger.appendChild(image);
  trigger.addEventListener("click", () => {
    openImageLightbox(imageUrl, imageLabel, trigger);
  });
  previewSourceMedia.appendChild(trigger);
}

function renderPreview() {
  const previewDoc = getPreviewDocument();
  const panelState = state.library.previewPanelState;
  const isMinimized = panelState === "minimized";
  const isClosed = panelState === "closed";

  browserPreviewPanel.classList.toggle("is-hidden", !previewDoc || isClosed);
  browserPreviewPanel.classList.toggle("is-minimized", isMinimized);
  browserPreviewPanel.setAttribute("aria-hidden", isClosed ? "true" : "false");
  previewContent.classList.toggle("is-hidden", isMinimized || isClosed);
  previewPanelTitle.textContent = previewDoc
    ? getDocumentDisplayLabel(previewDoc)
    : "Document preview";
  previewCard.style.zoom = String(state.library.previewZoom);
  previewZoomLabel.textContent = `${Math.round(state.library.previewZoom * 100)}%`;
  renderPreviewSourceMedia(previewDoc);
  minimizePreviewButton.textContent = isMinimized ? "+" : "−";
  minimizePreviewButton.setAttribute(
    "aria-label",
    isMinimized ? "Restore document preview" : "Minimize document preview"
  );
  minimizePreviewButton.title = isMinimized
    ? "Restore document preview"
    : "Minimize document preview";

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

// Modal lifecycle and context-scope commit ---------------------------------
function openDocumentBrowser(options = {}) {
  const scopeTarget = options.scopeTarget === "routine" ? "routine" : "chat";
  if (!state.auth.user || state.auth.user.must_change_password) {
    openUserManagement(openLibraryButton);
    return;
  }
  state.library.scopeSelectionTarget = scopeTarget;
  if (!state.library.loaded && !state.library.loadError) {
    state.library.collapseFoldersOnLoad = true;
    void loadDocumentLibrary();
  }
  if (
    canManageLibrary() &&
    !state.library.watchFoldersLoaded &&
    !state.library.watchFoldersLoadError
  ) {
    void loadWatchedFolders();
  } else if (!canManageLibrary()) {
    state.library.watchFolders = [];
    state.library.watchFoldersLoaded = true;
    state.library.watchFoldersLoadError = null;
  }
  state.library.editorDismissed = false;
  closeExplorerContextMenu();
  state.library.collapsedFolderIds = getAllLibraryFolderPathIds();
  state.library.draftContext = cloneContextFilter(
    scopeTarget === "routine" ? state.routines.draftContext : state.library.appliedContext
  );
  browserTitle.textContent = scopeTarget === "routine"
    ? "Select routine documents"
    : "Indexed Documents";
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
  if (state.library.scopeSelectionTarget === "routine") {
    state.routines.draftContext = cloneContextFilter(state.library.draftContext);
    state.routines.scopePickerOpen = false;
    renderRoutines();
  }
  state.library.scopeSelectionTarget = "chat";
  browserTitle.textContent = "Indexed Documents";
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

function commitDraftContextScope() {
  if (state.library.scopeSelectionTarget === "routine") {
    state.routines.draftContext = cloneContextFilter(state.library.draftContext);
    renderRoutines();
    return;
  }
  const nextContext = cloneContextFilter(state.library.draftContext);
  const changed = !contextFiltersEqual(nextContext, state.library.appliedContext);
  applyContextSelection(nextContext);
  if (changed) {
    persistActiveConversationPreferences();
    setLibraryActionStatus("Scope updated and remembered for the current conversation.", "success");
  }
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
        reasoning_mode: state.reasoningMode,
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

async function sendMessage(message, images = []) {
  if (!state.auth.user || state.auth.user.must_change_password) {
    openUserManagement(userLoginBadge);
    setConversationMemoryStatus("Sign in to start a private conversation.", "error");
    return;
  }
  if (
    (!message && images.length === 0) ||
    state.sending ||
    state.memory.pairDeleteInFlightIndex !== null
  ) {
    return;
  }

  renderMessage({
    role: "user",
    label: "You",
    body: message,
    images,
  });

  const requestId = crypto.randomUUID();
  const abortController = new AbortController();
  state.activeChatRequestId = requestId;
  state.activeChatAbortController = abortController;
  setComposerState(true);
  const datasheetProducts = message.match(
    /\b(?=[A-Z0-9-]*\d)[A-Z0-9]+(?:-[A-Z0-9]+)+\b/gi
  );
  const isDatasheetBatch =
    /data\s*sheet/i.test(message) && new Set(datasheetProducts || []).size >= 2;
  showResponsePreparationIndicator(
    isDatasheetBatch
      ? "Searching for product datasheets in parallel"
      : "Preparing response"
  );
  setConversationMemoryStatus("Preparing response. This chat will autosave after the reply.");
  const clientTimeout = window.setTimeout(() => {
    if (state.activeChatRequestId !== requestId) {
      return;
    }
    void fetch(`/api/chat/${encodeURIComponent(requestId)}/cancel`, {
      method: "POST",
    }).finally(() => {
      abortController.abort();
    });
  }, CHAT_CLIENT_TIMEOUT_MS);

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      signal: abortController.signal,
      body: JSON.stringify({
        request_id: requestId,
        conversation_id: state.conversationId,
        message,
        images: images.map((image) => ({
          filename: image.filename,
          mime_type: image.mime_type,
          content_base64: image.content_base64,
        })),
        source_mode: state.sourceMode,
        reasoning_mode: state.reasoningMode,
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
      generatedDocuments: payload.generated_documents || [],
    });
    await saveCurrentConversation({ silent: true });
  } catch (error) {
    hideResponsePreparationIndicator();
    const wasManuallyCancelled = state.cancelledChatRequestId === requestId;
    const failureMessage =
      error && error.name === "AbortError"
        ? wasManuallyCancelled
          ? "Response cancelled by the user."
          : "Response timed out and was cancelled."
        : error.message;
    renderMessage({
      role: "system",
      label: "System",
      body:
        failureMessage === "Response cancelled by the user."
          ? "Response cancelled."
          : `Request failed: ${failureMessage}`,
    });
    updateConversationMemoryStatus();
  } finally {
    window.clearTimeout(clientTimeout);
    if (state.activeChatRequestId === requestId) {
      state.activeChatRequestId = null;
      state.activeChatAbortController = null;
    }
    if (state.cancelledChatRequestId === requestId) {
      state.cancelledChatRequestId = null;
    }
    hideResponsePreparationIndicator();
    setComposerState(false);
    messageInput.focus();
  }
}

cancelResponseButton.addEventListener("click", async () => {
  const requestId = state.activeChatRequestId;
  if (!requestId || !state.sending) {
    return;
  }
  cancelResponseButton.disabled = true;
  cancelResponseButton.textContent = "Cancelling...";
  setConversationMemoryStatus(
    "Cancelling after the current external request finishes.",
    "warning"
  );
  try {
    const response = await fetch(`/api/chat/${encodeURIComponent(requestId)}/cancel`, {
      method: "POST",
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      throw new Error(payload?.detail || "Could not cancel the response.");
    }
    if (!payload.cancelled) {
      setConversationMemoryStatus(payload.message);
      return;
    }
    state.cancelledChatRequestId = requestId;
    state.activeChatAbortController?.abort();
  } catch (error) {
    cancelResponseButton.disabled = false;
    cancelResponseButton.textContent = "Cancel response";
    setConversationMemoryStatus(error.message, "error");
  }
});

addChatImagesButton.addEventListener("click", () => {
  if (!state.auth.user || state.auth.user.must_change_password) {
    openUserManagement(userLoginBadge);
    return;
  }
  chatImageInput.click();
});

chatImageInput.addEventListener("change", async () => {
  await addChatImageFiles(chatImageInput.files);
  chatImageInput.value = "";
});

messageInput.addEventListener("paste", async (event) => {
  const imageFiles = Array.from(event.clipboardData?.files || []).filter((file) =>
    file.type.startsWith("image/")
  );
  if (!imageFiles.length) {
    return;
  }
  event.preventDefault();
  await addChatImageFiles(imageFiles);
});

composerForm.addEventListener("dragenter", (event) => {
  if (Array.from(event.dataTransfer?.types || []).includes("Files")) {
    event.preventDefault();
    composerForm.classList.add("is-image-dragover");
  }
});

composerForm.addEventListener("dragover", (event) => {
  if (Array.from(event.dataTransfer?.types || []).includes("Files")) {
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = "copy";
    }
    composerForm.classList.add("is-image-dragover");
  }
});

composerForm.addEventListener("dragleave", (event) => {
  if (!composerForm.contains(event.relatedTarget)) {
    composerForm.classList.remove("is-image-dragover");
  }
});

composerForm.addEventListener("drop", async (event) => {
  composerForm.classList.remove("is-image-dragover");
  const imageFiles = Array.from(event.dataTransfer?.files || []).filter((file) =>
    file.type.startsWith("image/")
  );
  if (!imageFiles.length) {
    return;
  }
  event.preventDefault();
  await addChatImageFiles(imageFiles);
});

composerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = messageInput.value.trim();
  const images = state.chatImages.map((image) => ({ ...image }));
  if (!message && images.length === 0) {
    setConversationMemoryStatus("Enter a message or attach an image.", "error");
    return;
  }
  messageInput.value = "";
  clearChatImages();
  await sendMessage(message, images);
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
  if (!state.auth.user || state.auth.user.must_change_password) {
    openUserManagement(userLoginBadge);
    return;
  }
  closeSavedConversationContextMenu();
  resetConversation();
  messageInput.focus();
});

messageList.addEventListener("scroll", () => {
  if (state.memory.restoringScrollPosition) {
    return;
  }
  if (conversationScrollSaveTimer !== null) {
    window.clearTimeout(conversationScrollSaveTimer);
  }
  const conversationId = state.conversationId;
  conversationScrollSaveTimer = window.setTimeout(() => {
    conversationScrollSaveTimer = null;
    if (conversationId === state.conversationId) {
      rememberConversationScrollPosition(conversationId);
    }
  }, 120);
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

sourceModeButton.addEventListener("click", () => {
  const nextSourceMode = state.sourceMode === "broader" ? "internal" : "broader";
  applySourceMode(nextSourceMode);
  renderContextSummary();
  renderScopePane();
  persistActiveConversationPreferences();
  const sourceModeLabel = nextSourceMode === "broader" ? "Context: Global" : "Context: Internal";
  setConversationMemoryStatus(
    `${sourceModeLabel} is now active and remembered for this conversation.`,
    "success"
  );
});

reasoningModeButton.addEventListener("click", () => {
  const nextReasoningMode = state.reasoningMode === "maximum" ? "standard" : "maximum";
  applyReasoningMode(nextReasoningMode);
  persistActiveConversationPreferences();
  const modelLabel = nextReasoningMode === "maximum" ? "Terra" : "Luna";
  const reasoningLabel = nextReasoningMode === "maximum" ? "Maximum" : "Standard";
  setConversationMemoryStatus(
    `${reasoningLabel} reasoning (${modelLabel}) is now active and remembered for this conversation.`,
    "success"
  );
});

openLibraryButton.addEventListener("click", () => {
  closeSavedConversationContextMenu();
  openDocumentBrowser();
});

openRoutinesButton.addEventListener("click", () => {
  const routineWindow = window.open("/routines", "_blank");
  if (routineWindow) {
    routineWindow.focus();
  } else {
    setConversationMemoryStatus("Your browser blocked the Routines window. Allow pop-ups for Ask Jenny and try again.", "error");
  }
});

closeRoutinesButton.addEventListener("click", closeRoutines);
routinesBackdrop.addEventListener("click", closeRoutines);
routineScheduleSelect.addEventListener("change", () => {
  routineWeekdayField.classList.toggle("is-hidden", routineScheduleSelect.value !== "weekly");
});
routineSourceModeButton.addEventListener("click", () => {
  applyRoutineSourceMode(state.routines.draftSourceMode === "internal" ? "broader" : "internal");
  renderRoutines();
});
routineForm.addEventListener("submit", (event) => {
  event.preventDefault();
  void createRoutineFromForm();
});
routineScopePickerButton.addEventListener("click", () => {
  openRoutineScopePicker();
});

resetRoutineEditorButton.addEventListener("click", () => {
  resetRoutineEditor();
  setRoutineStatus("Ready to create a new routine.", "neutral");
  renderRoutines();
  routineNameInput.focus();
});

routineScopeSearchInput.addEventListener("input", () => {
  state.routines.scopeSearchQuery = routineScopeSearchInput.value;
  renderRoutineScopePicker();
});

clearRoutineScopeButton.addEventListener("click", () => {
  state.routines.draftContext = { folderIds: [], documentIds: [] };
  renderRoutines();
});

closeRoutineScopePickerButton.addEventListener("click", () => {
  state.routines.scopePickerOpen = false;
  renderRoutines();
});
routineSystemPauseButton.addEventListener("click", () => {
  void toggleRoutineSystemPause();
});

userLoginBadge.addEventListener("click", () => {
  openUserManagement(userLoginBadge);
});

closeUserManagementButton.addEventListener("click", () => {
  closeUserManagement();
});

userManagementBackdrop.addEventListener("click", () => {
  closeUserManagement();
});

showSignInButton.addEventListener("click", () => {
  setAuthView("signin");
});

showSignUpButton.addEventListener("click", () => {
  setAuthView("signup");
});

signInForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const succeeded = await submitAuth(
    "/api/auth/login",
    {
      username: signInUsernameInput.value.trim(),
      password: signInPasswordInput.value,
    },
    signInSubmitButton,
    "Signing in..."
  );
  if (succeeded) {
    signInPasswordInput.value = "";
  }
});

signUpForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (signUpPasswordInput.value !== signUpPasswordConfirmInput.value) {
    setUserManagementStatus("Passwords do not match.", "error");
    signUpPasswordConfirmInput.focus();
    return;
  }
  const succeeded = await submitAuth(
    "/api/auth/signup",
    {
      display_name: signUpDisplayNameInput.value.trim(),
      username: signUpUsernameInput.value.trim(),
      password: signUpPasswordInput.value,
    },
    signUpSubmitButton,
    "Creating account..."
  );
  if (succeeded) {
    signUpForm.reset();
  }
});

showChangePasswordButton.addEventListener("click", () => {
  setAuthView("change");
});

cancelChangePasswordButton.addEventListener("click", () => {
  setAuthView("account");
});

changePasswordForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (newPasswordInput.value !== newPasswordConfirmInput.value) {
    setUserManagementStatus("New passwords do not match.", "error");
    newPasswordConfirmInput.focus();
    return;
  }
  const succeeded = await submitAuth(
    "/api/auth/change-password",
    {
      current_password: currentPasswordInput.value,
      new_password: newPasswordInput.value,
    },
    changePasswordSubmitButton,
    "Saving..."
  );
  if (succeeded) {
    changePasswordForm.reset();
    state.auth.view = "account";
    renderAuth();
  }
});

signOutButton.addEventListener("click", () => {
  void signOut();
});

forcedSignOutButton.addEventListener("click", () => {
  void signOut();
});

closeBrowserButton.addEventListener("click", () => {
  closeDocumentBrowser();
});

minimizePreviewButton.addEventListener("click", () => {
  toggleDocumentPreviewMinimized();
});

closePreviewButton.addEventListener("click", () => {
  closeDocumentPreview();
});

browserPreviewPanel.addEventListener(
  "wheel",
  (event) => {
    if ((!event.ctrlKey && !event.metaKey) || state.library.previewPanelState !== "open") {
      return;
    }

    const previewDoc = getPreviewDocument();
    if (!previewDoc || event.deltaY === 0) {
      return;
    }

    event.preventDefault();
    const zoomDelta = event.deltaY < 0 ? PREVIEW_ZOOM_STEP : -PREVIEW_ZOOM_STEP;
    const nextZoom = Math.min(
      PREVIEW_ZOOM_MAX,
      Math.max(PREVIEW_ZOOM_MIN, state.library.previewZoom + zoomDelta)
    );
    state.library.previewZoom = Math.round(nextZoom * 10) / 10;
    previewCard.style.zoom = String(state.library.previewZoom);
    previewZoomLabel.textContent = `${Math.round(state.library.previewZoom * 100)}%`;
  },
  { passive: false }
);

document.querySelectorAll("[data-close-browser]").forEach((element) => {
  element.addEventListener("click", () => {
    closeDocumentBrowser();
  });
});

explorerRootButton.addEventListener("click", () => {
  setActiveFolder(null);
});

folderTreeList.addEventListener("click", handleFolderControlClick, true);

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

openSourceLocationButton.addEventListener("click", async () => {
  const watchedFolder = getWatchedFolderForLibraryFolder(state.library.activeFolderId);
  if (watchedFolder) {
    await openWatchedFolderSource(watchedFolder.watch_id);
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

documentEditorTagsInput.addEventListener("input", () => {
  renderDocumentEditorTags();
});

folderPropertiesTagsInput.addEventListener("input", () => {
  renderFolderPropertyTags();
});

imageLightboxBackdrop.addEventListener("click", () => {
  closeImageLightbox();
});

imageLightboxCloseButton.addEventListener("click", () => {
  closeImageLightbox();
});

document.addEventListener("click", (event) => {
  const imageLink = event.target instanceof Element
    ? event.target.closest(".rendered-markdown-image-link")
    : null;
  if (!imageLink) {
    return;
  }

  event.preventDefault();
  openImageLightbox(
    imageLink.getAttribute("href"),
    imageLink.dataset.imageAlt || "Referenced image",
    imageLink
  );
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !imageLightbox.classList.contains("is-hidden")) {
    event.preventDefault();
    closeImageLightbox();
    return;
  }
  if (event.key === "Escape" && state.memory.messageActionMenu.open) {
    event.preventDefault();
    closeMessageActionMenu();
    return;
  }
  if (event.key === "Escape" && state.memory.contextMenu.open) {
    event.preventDefault();
    closeSavedConversationContextMenu();
    return;
  }
  if (event.key === "Escape" && !userManagementModal.classList.contains("is-hidden")) {
    event.preventDefault();
    closeUserManagement();
    return;
  }
  if (event.key === "Escape" && !routinesModal.classList.contains("is-hidden")) {
    event.preventDefault();
    closeRoutines();
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
    state.memory.messageActionMenu.open &&
    state.memory.messageActionMenu.node &&
    !state.memory.messageActionMenu.node.contains(event.target) &&
    !state.memory.messageActionMenu.button?.contains(event.target)
  ) {
    closeMessageActionMenu();
  }
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
  closeMessageActionMenu();
  if (state.memory.contextMenu.open) {
    closeSavedConversationContextMenu();
  }
  if (state.library.contextMenu.open) {
    closeExplorerContextMenu();
  }
});

window.addEventListener("pagehide", () => {
  rememberActiveConversationScrollPosition();
  stopUiSessionHeartbeat();
});

window.addEventListener("pageshow", () => {
  startUiSessionHeartbeat();
});

documentBrowser.addEventListener("scroll", () => {
  if (state.library.contextMenu.open) {
    closeExplorerContextMenu();
  }
}, true);

applyReasoningMode(state.reasoningMode);
applySourceMode(state.sourceMode);
applyRoutineSourceMode(state.routines.draftSourceMode);
renderContextSummary();
renderDeleteSelectionSummary();
renderScopePane();
renderRoutines();
setLibraryActionStatus("Browse the library, adjust scope, or select a file to edit its metadata.");
setDocumentEditorStatus("Select a document to rename it, move it, or update its tags.");
setDocumentEditorState(false);
setDocumentGenerationState(false);
setDocumentGenerationStatus("Uses the active source mode and currently applied library scope.");
setUploadStatus("Choose files or a local folder, including Dropbox-synced folders, to import and embed in one pass.");
renderSavedConversationList();
renderConversationSaveButton();
resetConversation({ rememberCurrentScroll: false });
startUiSessionHeartbeat();
void initializeAuthenticatedWorkspace();
void loadDocumentLibrary();
