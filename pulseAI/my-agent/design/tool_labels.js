/**
 * tool_labels.js
 * --------------
 * Maps internal tool/function names (from tools.TOOL_FUNCTIONS) to
 * user-facing labels + icons for the webview UI, so the UI never leaks
 * implementation details like "write_file" or "run_command" to the user --
 * they see "Editing style.css" / "Running a command" instead.
 *
 * This is the REAL mapping the webview extension code should use (not just
 * mockup filler) -- it covers every tool name actually registered in
 * tools.py as of this session (confirmed directly: ran
 * `python3 -c "import tools; print(sorted(tools.TOOL_FUNCTIONS))"` and
 * matched every entry below against that real list, including the
 * optional git/RAG/AST/MCP/browser/web/image/LSP tools that only register
 * if their dependency is installed).
 *
 * Format: toolName -> { verb, icon, detail(args) }
 *   verb    - short present-progressive label shown while running
 *             ("Reading style.css") and past-tense once done is derived by
 *             the UI itself (see pastTense() below), not stored twice.
 *   icon    - a key into the ICONS svg map (webview_flat.html has the
 *             matching inline <symbol> defs) -- generic action icons
 *             (file, terminal, search, etc.), never the raw tool name.
 *   detail  - optional function(args) -> string for a short human
 *             argument summary (e.g. just the filename, not raw JSON).
 */

const TOOL_LABELS = {
  // --- Core file/command tools ---
  read_file:            { verb: "Reading",            icon: "file",      detail: a => a.path },
  write_file:           { verb: "Editing",             icon: "edit",      detail: a => a.path },
  undo_last_edit:       { verb: "Undoing last edit to", icon: "undo",     detail: a => a.path },
  list_files:           { verb: "Looking at",           icon: "folder",   detail: a => a.directory || "." },
  grep_files:           { verb: "Searching for",        icon: "search",   detail: a => `"${a.pattern}"` },
  run_command:          { verb: "Running a command",    icon: "terminal", detail: a => truncate(a.cmd, 40) },

  // --- Background process management ---
  start_background_process: { verb: "Starting",         icon: "play",     detail: a => a.name || truncate(a.cmd, 30) },
  stop_background_process:  { verb: "Stopping process", icon: "stop",     detail: a => a.handle },
  list_background_processes:{ verb: "Checking running processes", icon: "list", detail: () => "" },

  // --- Browser / visual verification ---
  screenshot_url:        { verb: "Taking a screenshot of", icon: "camera", detail: a => a.url },
  test_local_html:       { verb: "Previewing",           icon: "browser",  detail: a => a.file_path },
  evaluate_js:           { verb: "Checking the page",     icon: "browser", detail: () => "" },

  // --- Web search / image generation ---
  web_search:            { verb: "Searching the web for", icon: "search",  detail: a => `"${a.query}"` },
  generate_image:        { verb: "Generating an image",   icon: "image",   detail: a => truncate(a.prompt, 40) },

  // --- LSP (semantic code) ---
  lsp_find_references:  { verb: "Finding uses of",        icon: "link",    detail: a => a.symbol_name },
  lsp_preview_rename:   { verb: "Previewing a rename of",  icon: "edit",    detail: a => `${a.symbol_name} → ${a.new_name}` },
  lsp_get_diagnostics:  { verb: "Checking for errors in",  icon: "check",   detail: a => a.file_path },

  // --- RAG (semantic search) ---
  rag_index_directory:  { verb: "Indexing",                icon: "layers",  detail: a => a.directory || "." },
  rag_index_file:       { verb: "Re-indexing",              icon: "layers",  detail: a => a.path },
  rag_search:           { verb: "Searching the codebase for", icon: "search", detail: a => `"${a.query}"` },
  rag_index_stats:      { verb: "Checking the search index", icon: "layers", detail: () => "" },

  // --- AST / code transforms ---
  ast_transform_var_to_const:  { verb: "Modernizing variables in", icon: "wand",  detail: a => a.file_path },
  ast_add_jsdoc:               { verb: "Adding documentation to",  icon: "wand",  detail: a => a.function_name },
  ast_find_untyped_functions:  { verb: "Checking documentation in", icon: "wand", detail: a => a.file_path },

  // --- Git ---
  git_init:              { verb: "Setting up version control", icon: "git",  detail: () => "" },
  git_status:            { verb: "Checking what's changed",     icon: "git",  detail: () => "" },
  git_diff:              { verb: "Reviewing changes",            icon: "git",  detail: a => a.path || "" },
  git_commit:            { verb: "Saving a checkpoint",          icon: "git",  detail: a => truncate(a.message, 40) },
  git_log:               { verb: "Checking history",             icon: "git",  detail: () => "" },
  git_create_branch:     { verb: "Creating a branch",            icon: "git",  detail: a => a.branch_name },

  // --- MCP filesystem / fetch (external servers, still shown generically) ---
  fetch_fetch:                  { verb: "Fetching",              icon: "globe",  detail: a => a.url },
  filesystem_read_file:         { verb: "Reading",                icon: "file",   detail: a => a.path },
  filesystem_read_text_file:    { verb: "Reading",                icon: "file",   detail: a => a.path },
  filesystem_write_file:        { verb: "Editing",                icon: "edit",   detail: a => a.path },
  filesystem_list_directory:    { verb: "Looking at",             icon: "folder", detail: a => a.path },
  filesystem_directory_tree:    { verb: "Mapping",                icon: "folder", detail: a => a.path },
  filesystem_search_files:      { verb: "Searching for",          icon: "search", detail: a => a.pattern },
  filesystem_get_file_info:     { verb: "Checking",               icon: "file",   detail: a => a.path },
};

// Fallback for any tool name not in the map above (e.g. a new one added
// later without updating this file) -- NEVER falls back to showing the
// raw tool name; degrades to a generic, still-identity-hiding label.
const FALLBACK_LABEL = { verb: "Working", icon: "gear", detail: () => "" };

function getToolLabel(toolName, args) {
  const entry = TOOL_LABELS[toolName] || FALLBACK_LABEL;
  const detail = entry.detail ? entry.detail(args || {}) : "";
  return { verb: entry.verb, icon: entry.icon, detail };
}

function truncate(s, n) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n - 1) + "\u2026" : s;
}

// Present-progressive -> past-tense for "done" state, so the SAME verb
// list drives both the "running" and "completed" phrasing without a
// second table to keep in sync (e.g. "Reading style.css" while running ->
// "Read style.css" once done).
const PAST_TENSE_OVERRIDES = {
  "Reading": "Read",
  "Editing": "Edited",
  "Looking at": "Looked at",
  "Searching for": "Searched for",
  "Running a command": "Ran a command",
  "Starting": "Started",
  "Stopping process": "Stopped process",
  "Checking running processes": "Checked running processes",
  "Taking a screenshot of": "Took a screenshot of",
  "Previewing": "Previewed",
  "Checking the page": "Checked the page",
  "Searching the web for": "Searched the web for",
  "Generating an image": "Generated an image",
  "Finding uses of": "Found uses of",
  "Previewing a rename of": "Previewed a rename of",
  "Checking for errors in": "Checked for errors in",
  "Indexing": "Indexed",
  "Re-indexing": "Re-indexed",
  "Searching the codebase for": "Searched the codebase for",
  "Checking the search index": "Checked the search index",
  "Modernizing variables in": "Modernized variables in",
  "Adding documentation to": "Added documentation to",
  "Checking documentation in": "Checked documentation in",
  "Setting up version control": "Set up version control",
  "Checking what's changed": "Checked what's changed",
  "Reviewing changes": "Reviewed changes",
  "Saving a checkpoint": "Saved a checkpoint",
  "Checking history": "Checked history",
  "Creating a branch": "Created a branch",
  "Fetching": "Fetched",
  "Mapping": "Mapped",
  "Checking": "Checked",
  "Undoing last edit to": "Undid last edit to",
  "Working": "Finished",
};

function pastTense(verb) {
  return PAST_TENSE_OVERRIDES[verb] || verb;
}

if (typeof module !== "undefined") {
  module.exports = { TOOL_LABELS, getToolLabel, pastTense };
}
