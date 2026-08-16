import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

/**
 * Frontend for the LLM Widget node.
 *
 * The node's three text fields stay native ComfyUI multiline widgets on
 * purpose: they already handle IME composition, resizing and undo, which a
 * hand-rolled contenteditable would have to reimplement. This file only adds
 * the pieces ComfyUI has no widget for — a run/stop button that talks to the
 * editor-time HTTP route, a settings dialog, and the link tracing that turns a
 * connected IMAGE/VIDEO socket into a file the server can read.
 */

const NODE_NAME = "LLMWidget";
const ROUTE_PREFIX = "/llm_widget";
const SETTINGS_ENDPOINT = `${ROUTE_PREFIX}/settings`;
const GGUF_ENDPOINT = `${ROUTE_PREFIX}/gguf_models`;
const GENERATE_ENDPOINT = `${ROUTE_PREFIX}/generate`;
const CANCEL_ENDPOINT = `${ROUTE_PREFIX}/generate_cancel`;
const UNLOAD_ENDPOINT = `${ROUTE_PREFIX}/unload`;
const ANSWER_EVENT = "llm_widget/answer";

const FORMAT_OPENAI = "openai";
const FORMAT_GEMINI = "gemini";
const FORMAT_GGUF = "gguf";
const FORMATS = [FORMAT_OPENAI, FORMAT_GEMINI, FORMAT_GGUF];
const FORMAT_LABELS = {
    [FORMAT_OPENAI]: "OpenAI-compatible",
    [FORMAT_GEMINI]: "Gemini",
    [FORMAT_GGUF]: "GGUF (llama-cpp-python)",
};

const THINKING_OFF = "off";
const THINKING_KEEP = "keep";
const THINKING_MODES = [THINKING_OFF, THINKING_KEEP];
const THINKING_LABELS = {
    [THINKING_OFF]: "Off — ask the model not to reason",
    [THINKING_KEEP]: "Keep — show the reasoning in the answer",
};

// Continue mode turns the answer field into an IRC-style log. The markers are
// the server's, and only a line that starts with one opens a turn.
const CHAT_USER_MARK = "<you>";
const CHAT_MODEL_MARK = "<llm>";
const CHAT_TURN_RE = new RegExp(`^(${CHAT_USER_MARK}|${CHAT_MODEL_MARK})[ \\t]*`, "gm");
const HISTORY_TURNS_DEFAULT = 8;
const HISTORY_TURNS_MIN = 1;
const HISTORY_TURNS_LIMIT = 64;

const MAX_LENGTH_DEFAULT = 1024;
const MAX_LENGTH_MIN = 16;
const MAX_LENGTH_LIMIT = 32768;
const GGUF_MMPROJ_AUTO = "auto";
const GGUF_MMPROJ_NONE = "none";
const GGUF_CONTEXT_DEFAULT = 16384;
const GGUF_CONTEXT_MIN = 512;
const GGUF_CONTEXT_LIMIT = 1048576;

const MEDIA_INPUTS = [
    { name: "image", type: "image", icon: "▣" },
    { name: "video", type: "video", icon: "▶" },
];
const MEDIA_EXTENSIONS = /\.(png|jpe?g|webp|gif|bmp|tiff?|mp4|webm|mov|mkv|avi|m4v)$/i;
const ANNOTATED_PATH_RE = /\s*\[(input|output|temp)\]\s*$/i;

// Mirrors VIDEO_SAMPLES in nodes.py: how many stills a connected video becomes
// before it is shown to the model. The server samples candidates at 1 fps,
// always keeps the first and the last frame, and spends what is left of the
// budget on the frames that changed most from the one before them. The values
// are the canonical ids the server accepts; the labels are display only.
const VIDEO_SAMPLE_MIN = 2;
const VIDEO_SAMPLE_MAX = 12;
const VIDEO_SAMPLE_DEFAULT = "4frames";
const VIDEO_SAMPLES = Array.from(
    { length: VIDEO_SAMPLE_MAX - VIDEO_SAMPLE_MIN + 1 },
    (_unused, index) => {
        const count = VIDEO_SAMPLE_MIN + index;
        return { value: `${count}frames`, label: `${count} frames` };
    },
);

const SETTINGS_DEFAULTS = Object.freeze({
    api_format: FORMAT_OPENAI,
    api_url: "",
    api_key: "",
    model: "",
    read_media: true,
    video_sample: VIDEO_SAMPLE_DEFAULT,
    thinking: THINKING_OFF,
    temperature: 0.35,
    max_length: MAX_LENGTH_DEFAULT,
    continue_chat: false,
    history_turns: HISTORY_TURNS_DEFAULT,
    chat_blank_lines: true,
    gguf_model: "",
    gguf_mmproj: GGUF_MMPROJ_AUTO,
    gguf_context: GGUF_CONTEXT_DEFAULT,
    gguf_gpu_layers: -1,
    gguf_unload_after: false,
    gguf_describe_media: false,
});

const TEXT = {
    title: "LLM Widget settings",
    run: "Ask the model",
    stop: "Stop",
    copy: "Copy the answer",
    copied: "Answer copied",
    clear: "Clear the conversation",
    clearConfirm: "Clear the conversation log in the answer field?",
    cleared: "Conversation cleared",
    unload: "Unload the model",
    unloadHttp: "Unload the model (LM Studio / Ollama)",
    unloaded: "Model unloaded",
    unloadIdle: "No model was loaded",
    settings: "Settings",
    running: "Thinking",
    apiFormat: "API format",
    apiUrl: "API URL",
    apiKey: "API key",
    model: "Model",
    temperature: "Temperature",
    maxLength: "Max answer tokens",
    ggufModel: "GGUF model",
    ggufMmproj: "Vision projector",
    ggufContext: "Context size",
    ggufGpuLayers: "GPU layers (-1 = all)",
    ggufUnload: "Unload the model after answering",
    ggufDescribe: "Describe each media in its own pass",
    readMedia: "Send the connected image / video",
    videoSample: "Video frames sent",
    videoSampleHint:
        "A chat request has no video channel, so a connected video is sent as stills. Candidates are "
        + "taken at one per second; the first and the last frame are always kept and the rest of the "
        + "budget goes to the frames that changed most, with their timestamps stated in the request.",
    continueChat: "Continue the conversation",
    historyTurns: "Exchanges kept in the history",
    chatBlankLines: "Blank line between the log entries",
    continueHint:
        "Every turn is appended to the answer field as an IRC-style log, and the exchanges above the "
        + "question are sent with it. The field stays a plain text box: edit or delete lines to change "
        + "what the model remembers. Connected media is attached to the current question only, and the "
        + "node's text output carries the last <llm> block alone.",
    thinking: "Model thinking",
    thinkingHint:
        "Off sends the switches each backend understands and removes any <think> block the model "
        + "returns anyway. Reasoning that is emitted as plain prose cannot be separated from the "
        + "answer — if it still leaks, use the model's non-thinking variant.",
    save: "Save",
    close: "Close",
    saved: "Settings saved",
    discard: "The settings have been changed but not saved.\n\nClose and discard the changes?",
    loadFailed: "Could not load the settings",
    failed: "LLM Widget",
    cancelled: "Generation stopped",
    missingHttp: "Set the API URL, key and model in the settings first",
    missingGguf: "Select a GGUF model in the settings first",
    emptyQuestion: "Type a question first",
    ggufEmpty: "No .gguf found under models/text_encoders or models/LLM",
    ggufAuto: "auto (next to the model)",
    ggufNone: "none (text only)",
    ggufHint:
        "Runs locally through llama-cpp-python. Reading an image needs a matching mmproj projector; "
        + "without one the model answers from text alone.",
    httpHint: "Any OpenAI-compatible /v1/chat/completions endpoint. The path is completed automatically.",
    geminiHint: "Google's native generateContent endpoint. Videos are sent whole instead of as sampled frames.",
    mediaNoFile: "not a saved file - it will be skipped",
    mediaSkipped: "Could not read: ",
};

let settingsCache = { ...SETTINGS_DEFAULTS };
let settingsLoaded = false;
let settingsPromise = null;
let settingsModal = null;

// --------------------------------------------------------------------------- //
// Settings transport
// --------------------------------------------------------------------------- //

function asBoolean(value, fallback = false) {
    if (value == null) return fallback;
    if (typeof value === "string") return ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
    return Boolean(value);
}

function clampNumber(raw, fallback, low, high, round = true) {
    const number = Number(raw);
    if (!Number.isFinite(number)) return fallback;
    return Math.min(high, Math.max(low, round ? Math.round(number) : number));
}

function canonicalVideoSample(value) {
    const requested = String(value || "").trim().toLowerCase();
    return VIDEO_SAMPLES.some((item) => item.value === requested) ? requested : VIDEO_SAMPLE_DEFAULT;
}

function normalizeSettings(value) {
    const source = value && typeof value === "object" ? value : {};
    const requested = String(source.api_format || FORMAT_OPENAI).toLowerCase();
    return {
        api_format: FORMATS.includes(requested) ? requested : FORMAT_OPENAI,
        api_url: String(source.api_url || "").trim(),
        api_key: String(source.api_key || ""),
        model: String(source.model || "").trim(),
        read_media: asBoolean(source.read_media, true),
        video_sample: canonicalVideoSample(source.video_sample),
        thinking: THINKING_MODES.includes(String(source.thinking || "").toLowerCase())
            ? String(source.thinking).toLowerCase()
            : THINKING_OFF,
        temperature: clampNumber(source.temperature, SETTINGS_DEFAULTS.temperature, 0, 2, false),
        max_length: clampNumber(source.max_length, MAX_LENGTH_DEFAULT, MAX_LENGTH_MIN, MAX_LENGTH_LIMIT),
        continue_chat: asBoolean(source.continue_chat, false),
        history_turns: clampNumber(source.history_turns, HISTORY_TURNS_DEFAULT, HISTORY_TURNS_MIN, HISTORY_TURNS_LIMIT),
        chat_blank_lines: asBoolean(source.chat_blank_lines, true),
        gguf_model: String(source.gguf_model || "").trim(),
        gguf_mmproj: String(source.gguf_mmproj || GGUF_MMPROJ_AUTO).trim() || GGUF_MMPROJ_AUTO,
        gguf_context: clampNumber(source.gguf_context, GGUF_CONTEXT_DEFAULT, GGUF_CONTEXT_MIN, GGUF_CONTEXT_LIMIT),
        gguf_gpu_layers: clampNumber(source.gguf_gpu_layers, -1, -1, 1024),
        gguf_unload_after: asBoolean(source.gguf_unload_after, false),
        gguf_describe_media: asBoolean(source.gguf_describe_media, false),
    };
}

async function loadSettings({ force = false } = {}) {
    if (settingsPromise && !force) return settingsPromise;
    if (settingsLoaded && !force) return settingsCache;
    settingsPromise = (async () => {
        const response = await api.fetchApi(SETTINGS_ENDPOINT);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data?.ok) throw new Error(data?.error || `HTTP ${response.status}`);
        settingsCache = normalizeSettings(data.settings);
        settingsLoaded = true;
        syncAllNodes();
        return settingsCache;
    })().finally(() => {
        settingsPromise = null;
    });
    return settingsPromise;
}

async function saveSettings(value) {
    const settings = normalizeSettings(value);
    const response = await api.fetchApi(SETTINGS_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data?.ok) throw new Error(data?.error || `HTTP ${response.status}`);
    settingsCache = normalizeSettings(data.settings || settings);
    settingsLoaded = true;
    syncAllNodes();
    return settingsCache;
}

async function loadGgufCatalog() {
    const response = await api.fetchApi(GGUF_ENDPOINT);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data?.ok) throw new Error(data?.error || `HTTP ${response.status}`);
    return {
        models: Array.isArray(data.models) ? data.models : [],
        mmproj: Array.isArray(data.mmproj) ? data.mmproj : [],
        roots: Array.isArray(data.roots) ? data.roots : [],
    };
}

function notify(message, severity = "error") {
    const detail = String(message || "");
    const toast = app.extensionManager?.toast;
    if (toast?.add) {
        toast.add({ severity, summary: TEXT.failed, detail, life: 6000 });
        return;
    }
    if (severity === "error") globalThis.alert?.(detail);
}

// --------------------------------------------------------------------------- //
// Node helpers
// --------------------------------------------------------------------------- //

function isTarget(node) {
    return String(node?.comfyClass || node?.type || "") === NODE_NAME;
}

function targetNodes() {
    return (app.graph?._nodes || []).filter(isTarget);
}

function getWidget(node, name) {
    return (node?.widgets || []).find((widget) => String(widget?.name || "") === name) || null;
}

function widgetText(node, name) {
    return String(getWidget(node, name)?.value ?? "");
}

function setWidgetText(node, name, value) {
    const widget = getWidget(node, name);
    if (!widget) return;
    const text = String(value ?? "");
    widget.value = text;
    // Multiline widgets are backed by a textarea whose DOM value does not
    // always follow the property assignment on every frontend version.
    if (widget.element && "value" in widget.element) widget.element.value = text;
    if (widget.inputEl && "value" in widget.inputEl) widget.inputEl.value = text;
    try {
        widget.callback?.(text, app.canvas, node);
    } catch (error) {
        /* a widget that refuses the callback still holds the value */
    }
    node.setDirtyCanvas?.(true, true);
    app.graph?.change?.();
}

/** The textarea behind a multiline widget, wherever this frontend keeps it. */
function widgetField(node, name) {
    const widget = getWidget(node, name);
    const host = widget?.inputEl || widget?.element;
    if (!host) return null;
    return host.tagName === "TEXTAREA" ? host : host.querySelector?.("textarea") || null;
}

/**
 * The reply inside the answer field: its last `<llm>` block.
 *
 * The server owns the same rule for what it sends the node's text output; this
 * copy is only so the copy button hands over the answer rather than the whole
 * conversation. A field with no markers is a plain answer and comes back whole.
 */
function lastAnswer(text) {
    const value = String(text ?? "");
    const marks = [...value.matchAll(CHAT_TURN_RE)];
    if (!marks.length) return value.trim();
    for (let index = marks.length - 1; index >= 0; index -= 1) {
        if (marks[index][1] !== CHAT_MODEL_MARK) continue;
        const end = index + 1 < marks.length ? marks[index + 1].index : value.length;
        const content = value.slice(marks[index].index + marks[index][0].length, end).trim();
        if (content) return content;
    }
    return "";
}

/** Resolve one of this node's inputs to the graph node that actually feeds it. */
function inputSourceNode(node, inputName) {
    const slot = (node?.inputs || []).find((input) => String(input?.name || "") === inputName);
    if (!slot || slot.link == null) return null;
    let link = app.graph?.links?.[slot.link];
    // Reroutes carry no file of their own, so walk through them to the loader.
    for (let guard = 0; link && guard < 32; guard += 1) {
        const origin = app.graph?.getNodeById?.(Number(link.origin_id));
        if (!origin) return null;
        const kind = String(origin.comfyClass || origin.type || "");
        if (!/reroute/i.test(kind)) return origin;
        const upstream = (origin.inputs || [])[0];
        if (upstream?.link == null) return origin;
        link = app.graph?.links?.[upstream.link];
    }
    return null;
}

/**
 * Find the file a loader node points at.
 *
 * Only an asset that already lives in ComfyUI's input/output/temp folder can be
 * read from the editor: a tensor produced mid-graph has no file until the graph
 * runs. Nodes whose widgets hold no filename simply return null, and the run is
 * told what it had to skip rather than silently answering about nothing.
 */
function sourceAsset(node, mediaType) {
    if (!node) return null;
    const preferred = {
        image: ["image", "filename", "file"],
        video: ["video", "file", "filename", "video_file", "videofile"],
    }[mediaType] || ["file", "filename"];
    const preferredSet = new Set(preferred);
    const widgets = Array.isArray(node.widgets) ? node.widgets : [];
    const ordered = [
        ...widgets.filter((widget) => preferredSet.has(String(widget?.name || "").toLowerCase())),
        ...widgets,
    ];
    for (const widget of ordered) {
        const value = widget?.value;
        let filename = typeof value === "object" ? String(value?.filename || value?.name || "") : String(value ?? "");
        if (!filename || /^data:|^blob:|^https?:/i.test(filename)) continue;
        // LoadImage-style widgets annotate the folder inline ("photo.png [input]").
        let storage = typeof value === "object" ? String(value?.type || "input") : "input";
        const annotated = filename.match(ANNOTATED_PATH_RE);
        if (annotated) {
            storage = annotated[1].toLowerCase();
            filename = filename.replace(ANNOTATED_PATH_RE, "");
        }
        if (!preferredSet.has(String(widget?.name || "").toLowerCase()) && !MEDIA_EXTENSIONS.test(filename)) continue;
        return {
            filename,
            subfolder: typeof value === "object" ? String(value?.subfolder || "") : "",
            storage,
        };
    }
    return null;
}

function sourceLabel(node) {
    return String(node?.title || node?.comfyClass || node?.type || "node");
}

/** What the run button will send, in the order the server tags it. */
function mediaResources(node) {
    const resources = [];
    for (const { name, type, icon } of MEDIA_INPUTS) {
        const source = inputSourceNode(node, name);
        if (!source) continue;
        const asset = sourceAsset(source, type);
        resources.push({
            type,
            tag: `${type} 1`,
            icon,
            name: asset?.filename || sourceLabel(source),
            asset,
        });
    }
    return resources;
}

// --------------------------------------------------------------------------- //
// Settings dialog widgets
// --------------------------------------------------------------------------- //

function makeRow(labelText, control) {
    const row = document.createElement("div");
    row.className = "llmw-row";
    const label = document.createElement("span");
    label.className = "llmw-label";
    label.textContent = labelText;
    control.setAttribute?.("aria-label", labelText);
    row.append(label, control);
    return row;
}

function makeSelect(initialValue, onChange = null, optionList = null) {
    const root = document.createElement("div");
    root.className = "llmw-select-wrap";
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "llmw-control llmw-select";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    const valueLabel = document.createElement("span");
    valueLabel.className = "llmw-select-value";
    const chevron = document.createElement("span");
    chevron.className = "llmw-select-chevron";
    const menu = document.createElement("div");
    menu.className = "llmw-select-menu";
    menu.setAttribute("role", "listbox");
    menu.hidden = true;
    const options = optionList || FORMATS.map((value) => ({ value, label: FORMAT_LABELS[value] }));
    trigger.value = options.some((item) => item.value === initialValue) ? initialValue : options[0]?.value || "";
    let activeIndex = Math.max(0, options.findIndex((item) => item.value === trigger.value));

    const close = () => {
        menu.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
        trigger.classList.remove("is-open");
    };
    const open = () => {
        menu.hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        trigger.classList.add("is-open");
    };
    const render = () => {
        const current = options.find((item) => item.value === trigger.value) || options[0];
        valueLabel.textContent = current?.label || "";
        valueLabel.title = current?.label || "";
        for (const option of menu.querySelectorAll(".llmw-select-option")) {
            const selected = option.dataset.value === trigger.value;
            option.classList.toggle("is-selected", selected);
            option.setAttribute("aria-selected", selected ? "true" : "false");
        }
    };
    const choose = (value) => {
        const nextIndex = options.findIndex((item) => item.value === value);
        if (nextIndex < 0) return;
        trigger.value = value;
        activeIndex = nextIndex;
        render();
        close();
        trigger.focus();
        onChange?.(value);
    };
    const buildOptions = () => {
        menu.textContent = "";
        options.forEach((item, index) => {
            const option = document.createElement("button");
            option.type = "button";
            option.className = "llmw-select-option";
            option.dataset.value = item.value;
            option.setAttribute("role", "option");
            option.textContent = item.label;
            option.title = item.label;
            option.addEventListener("click", () => choose(item.value));
            option.addEventListener("pointerenter", () => { activeIndex = index; });
            menu.append(option);
        });
    };
    buildOptions();
    trigger.append(valueLabel, chevron);
    root.append(trigger, menu);
    trigger.addEventListener("click", () => (menu.hidden ? open() : close()));
    trigger.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            close();
            return;
        }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            if (menu.hidden) open();
            activeIndex = (activeIndex + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
            choose(options[activeIndex].value);
        }
        if ((event.key === "Enter" || event.key === " ") && !menu.hidden) {
            event.preventDefault();
            choose(options[activeIndex].value);
        }
    });
    Object.defineProperty(root, "value", {
        configurable: true,
        get: () => trigger.value,
        set: (value) => {
            if (options.some((item) => item.value === value)) {
                trigger.value = value;
                activeIndex = options.findIndex((item) => item.value === value);
                render();
            }
        },
    });
    root.focus = () => trigger.focus();
    root.closeMenu = close;
    // Lets a caller swap in options that are only known after a fetch.
    root.setOptions = (next) => {
        options.length = 0;
        options.push(...next);
        buildOptions();
        if (!options.some((item) => item.value === trigger.value)) trigger.value = options[0]?.value || "";
        activeIndex = Math.max(0, options.findIndex((item) => item.value === trigger.value));
        render();
    };
    render();
    return root;
}

function makeSwitch(initialValue, onChange = null) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "llmw-switch";
    toggle.role = "switch";
    toggle.checked = Boolean(initialValue);
    const track = document.createElement("span");
    track.className = "llmw-switch-track";
    const thumb = document.createElement("span");
    thumb.className = "llmw-switch-thumb";
    track.append(thumb);
    toggle.append(track);
    const render = () => {
        toggle.setAttribute("aria-checked", toggle.checked ? "true" : "false");
        toggle.classList.toggle("is-on", toggle.checked);
    };
    toggle.addEventListener("click", () => {
        toggle.checked = !toggle.checked;
        render();
        onChange?.(toggle.checked);
    });
    render();
    return toggle;
}

function makeCheckRow(labelText, toggle) {
    const row = document.createElement("div");
    row.className = "llmw-check";
    const text = document.createElement("span");
    text.textContent = labelText;
    toggle.setAttribute("aria-label", labelText);
    row.append(text, toggle);
    return row;
}

function makeNumberInput(value, min, max, step) {
    const input = document.createElement("input");
    input.className = "llmw-control";
    input.type = "number";
    input.min = String(min);
    input.max = String(max);
    input.step = String(step);
    input.value = String(value);
    return input;
}

/** The two fixed projector choices, plus `selected` when it is a real filename. */
function mmprojOptions(selected, catalog = []) {
    const options = [
        { value: GGUF_MMPROJ_AUTO, label: TEXT.ggufAuto },
        { value: GGUF_MMPROJ_NONE, label: TEXT.ggufNone },
        ...catalog.map((value) => ({ value, label: value })),
    ];
    const wanted = String(selected || "").trim();
    // A configured file the scan no longer finds stays listed rather than
    // silently becoming a different projector; the server reports it clearly.
    if (wanted && !options.some((item) => item.value === wanted)) {
        options.push({ value: wanted, label: wanted });
    }
    return options;
}

async function openSettings() {
    if (settingsModal) {
        settingsModal.dialog?.querySelector?.("input, button")?.focus?.();
        return;
    }
    try {
        await loadSettings();
    } catch (error) {
        notify(error?.message || TEXT.loadFailed);
    }

    const overlay = document.createElement("div");
    overlay.className = "llmw-overlay";
    const dialog = document.createElement("section");
    dialog.className = "llmw-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-label", TEXT.title);

    const header = document.createElement("div");
    header.className = "llmw-header";
    const title = document.createElement("div");
    title.className = "llmw-title";
    title.textContent = TEXT.title;
    const headerActions = document.createElement("div");
    headerActions.className = "llmw-header-actions";
    header.append(title, headerActions);

    const form = document.createElement("form");
    form.id = "llmw-settings-form";
    form.className = "llmw-form";

    const apiFormat = makeSelect(settingsCache.api_format, () => syncFormatRows());
    const apiUrl = document.createElement("input");
    apiUrl.className = "llmw-control";
    apiUrl.type = "text";
    apiUrl.autocomplete = "url";
    apiUrl.placeholder = "http://127.0.0.1:1234/v1";
    apiUrl.value = settingsCache.api_url;
    const apiKey = document.createElement("input");
    apiKey.className = "llmw-control";
    apiKey.type = "password";
    apiKey.autocomplete = "off";
    apiKey.spellcheck = false;
    apiKey.value = settingsCache.api_key;
    const model = document.createElement("input");
    model.className = "llmw-control";
    model.type = "text";
    model.autocomplete = "off";
    model.value = settingsCache.model;
    const thinking = makeSelect(
        settingsCache.thinking,
        null,
        THINKING_MODES.map((value) => ({ value, label: THINKING_LABELS[value] })),
    );
    const temperature = makeNumberInput(settingsCache.temperature, 0, 2, 0.05);
    const maxLength = makeNumberInput(settingsCache.max_length, MAX_LENGTH_MIN, MAX_LENGTH_LIMIT, 16);
    const historyTurns = makeNumberInput(settingsCache.history_turns, HISTORY_TURNS_MIN, HISTORY_TURNS_LIMIT, 1);
    const ggufContext = makeNumberInput(settingsCache.gguf_context, GGUF_CONTEXT_MIN, GGUF_CONTEXT_LIMIT, 512);
    const ggufGpuLayers = makeNumberInput(settingsCache.gguf_gpu_layers, -1, 1024, 1);
    const ggufModel = makeSelect(settingsCache.gguf_model, null, [
        { value: settingsCache.gguf_model, label: settingsCache.gguf_model || TEXT.ggufEmpty },
    ]);
    // The saved projector has to be in the list from the start. makeSelect drops
    // an initial value it cannot find and falls back to the first option, so
    // seeding only auto/none silently reset a configured projector to auto —
    // and the catalog fetch below then "restored" that reset value.
    const ggufMmproj = makeSelect(settingsCache.gguf_mmproj, null, mmprojOptions(settingsCache.gguf_mmproj));
    const videoSample = makeSelect(settingsCache.video_sample, null, VIDEO_SAMPLES);
    const readMedia = makeSwitch(settingsCache.read_media, () => syncFormatRows());
    const continueChat = makeSwitch(settingsCache.continue_chat, () => syncFormatRows());
    const chatBlankLines = makeSwitch(settingsCache.chat_blank_lines);
    const ggufUnload = makeSwitch(settingsCache.gguf_unload_after);
    const ggufDescribe = makeSwitch(settingsCache.gguf_describe_media);

    const hint = document.createElement("p");
    hint.className = "llmw-hint";
    const apiUrlRow = makeRow(TEXT.apiUrl, apiUrl);
    const apiKeyRow = makeRow(TEXT.apiKey, apiKey);
    const modelRow = makeRow(TEXT.model, model);
    const thinkingRow = makeRow(TEXT.thinking, thinking);
    const thinkingHint = document.createElement("p");
    thinkingHint.className = "llmw-hint";
    thinkingHint.textContent = TEXT.thinkingHint;
    const temperatureRow = makeRow(TEXT.temperature, temperature);
    const maxLengthRow = makeRow(TEXT.maxLength, maxLength);
    const ggufModelRow = makeRow(TEXT.ggufModel, ggufModel);
    const ggufMmprojRow = makeRow(TEXT.ggufMmproj, ggufMmproj);
    const ggufContextRow = makeRow(TEXT.ggufContext, ggufContext);
    const ggufGpuLayersRow = makeRow(TEXT.ggufGpuLayers, ggufGpuLayers);
    const readMediaRow = makeCheckRow(TEXT.readMedia, readMedia);
    const videoSampleRow = makeRow(TEXT.videoSample, videoSample);
    const videoSampleHint = document.createElement("p");
    videoSampleHint.className = "llmw-hint";
    videoSampleHint.textContent = TEXT.videoSampleHint;
    const continueChatRow = makeCheckRow(TEXT.continueChat, continueChat);
    const historyTurnsRow = makeRow(TEXT.historyTurns, historyTurns);
    const chatBlankLinesRow = makeCheckRow(TEXT.chatBlankLines, chatBlankLines);
    const continueHint = document.createElement("p");
    continueHint.className = "llmw-hint";
    continueHint.textContent = TEXT.continueHint;
    const ggufUnloadRow = makeCheckRow(TEXT.ggufUnload, ggufUnload);
    const ggufDescribeRow = makeCheckRow(TEXT.ggufDescribe, ggufDescribe);

    form.append(
        makeRow(TEXT.apiFormat, apiFormat),
        hint,
        apiUrlRow,
        apiKeyRow,
        modelRow,
        ggufModelRow,
        ggufMmprojRow,
        ggufContextRow,
        ggufGpuLayersRow,
        thinkingRow,
        thinkingHint,
        temperatureRow,
        maxLengthRow,
        continueChatRow,
        historyTurnsRow,
        chatBlankLinesRow,
        continueHint,
        ggufUnloadRow,
        readMediaRow,
        ggufDescribeRow,
        videoSampleRow,
        videoSampleHint,
    );

    const error = document.createElement("div");
    error.className = "llmw-error";
    error.hidden = true;

    // Each format uses a different half of this form, so only its own rows stay
    // visible. Sending the connected media applies to all of them.
    let ggufCatalogLoaded = false;
    const refreshGgufOptions = () => {
        if (ggufCatalogLoaded) return;
        ggufCatalogLoaded = true;
        loadGgufCatalog().then((catalog) => {
            // Read the current choices first: setOptions drops a value the new
            // list does not contain, so both lists are built to include it.
            const selectedModel = ggufModel.value;
            const models = catalog.models.map((value) => ({ value, label: value }));
            if (selectedModel && !catalog.models.includes(selectedModel)) {
                models.push({ value: selectedModel, label: selectedModel });
            }
            ggufModel.setOptions(models.length ? models : [{ value: "", label: TEXT.ggufEmpty }]);
            ggufModel.value = selectedModel;
            const selectedMmproj = ggufMmproj.value;
            ggufMmproj.setOptions(mmprojOptions(selectedMmproj, catalog.mmproj));
            ggufMmproj.value = selectedMmproj;
        }).catch((catalogError) => {
            ggufCatalogLoaded = false;
            error.textContent = String(catalogError?.message || catalogError);
            error.hidden = false;
        });
    };
    const syncFormatRows = () => {
        const gguf = apiFormat.value === FORMAT_GGUF;
        const gemini = apiFormat.value === FORMAT_GEMINI;
        for (const row of [apiUrlRow, apiKeyRow, modelRow]) row.hidden = gguf;
        for (const row of [ggufModelRow, ggufMmprojRow, ggufContextRow, ggufGpuLayersRow, ggufUnloadRow]) row.hidden = !gguf;
        // Describing media one at a time only means something once media is sent.
        ggufDescribeRow.hidden = !gguf || !readMedia.checked;
        // So does how many frames a video is thinned to.
        videoSampleRow.hidden = !readMedia.checked;
        videoSampleHint.hidden = !readMedia.checked;
        // How much history to replay, and how the log is spaced, are only
        // questions once there is a log at all.
        historyTurnsRow.hidden = !continueChat.checked;
        chatBlankLinesRow.hidden = !continueChat.checked;
        hint.textContent = gguf ? TEXT.ggufHint : gemini ? TEXT.geminiHint : TEXT.httpHint;
        apiUrl.placeholder = gemini ? "https://generativelanguage.googleapis.com" : "http://127.0.0.1:1234/v1";
        if (gguf) refreshGgufOptions();
    };
    syncFormatRows();

    const saveButton = document.createElement("button");
    saveButton.type = "submit";
    saveButton.className = "llmw-button is-header";
    saveButton.setAttribute("form", form.id);
    saveButton.textContent = TEXT.save;
    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "llmw-close";
    closeButton.textContent = "×";
    closeButton.title = TEXT.close;
    closeButton.setAttribute("aria-label", TEXT.close);
    headerActions.append(saveButton, closeButton);
    dialog.append(header, form, error);
    overlay.append(dialog);
    document.body.append(overlay);

    const formValues = () => ({
        api_format: apiFormat.value,
        api_url: apiUrl.value,
        api_key: apiKey.value,
        model: model.value,
        read_media: readMedia.checked,
        video_sample: videoSample.value,
        thinking: thinking.value,
        temperature: temperature.value,
        max_length: maxLength.value,
        continue_chat: continueChat.checked,
        history_turns: historyTurns.value,
        chat_blank_lines: chatBlankLines.checked,
        gguf_model: ggufModel.value,
        gguf_mmproj: ggufMmproj.value,
        gguf_context: ggufContext.value,
        gguf_gpu_layers: ggufGpuLayers.value,
        gguf_unload_after: ggufUnload.checked,
        gguf_describe_media: ggufDescribe.checked,
    });
    // Compared through normalizeSettings so the number inputs' string values and
    // a clamped-away edit do not count as a change.
    const pristine = JSON.stringify(normalizeSettings(formValues()));
    const isDirty = () => JSON.stringify(normalizeSettings(formValues())) !== pristine;

    const close = () => {
        document.removeEventListener("keydown", onKeyDown, true);
        overlay.remove();
        settingsModal = null;
    };
    /** Closing by ×, Escape or the backdrop asks first when nothing was saved. */
    const requestClose = () => {
        const ask = globalThis.confirm;
        // A host without window.confirm must not trap the dialog open.
        if (isDirty() && typeof ask === "function" && !ask.call(globalThis, TEXT.discard)) return;
        close();
    };
    const onKeyDown = (event) => {
        if (event.key === "Escape") {
            event.preventDefault();
            requestClose();
        }
    };
    settingsModal = { dialog, close };
    document.addEventListener("keydown", onKeyDown, true);
    overlay.addEventListener("pointerdown", (event) => {
        for (const select of [apiFormat, thinking, ggufModel, ggufMmproj, videoSample]) {
            if (!select.contains?.(event.target)) select.closeMenu?.();
        }
        if (event.target === overlay) requestClose();
    });
    closeButton.addEventListener("click", requestClose);
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (saveButton.disabled) return;
        saveButton.disabled = true;
        error.hidden = true;
        try {
            await saveSettings(formValues());
            notify(TEXT.saved, "success");
            close();
        } catch (saveError) {
            error.textContent = String(saveError?.message || saveError || TEXT.failed);
            error.hidden = false;
            saveButton.disabled = false;
        }
    });
    apiFormat.focus();
}

// --------------------------------------------------------------------------- //
// Toolbar
// --------------------------------------------------------------------------- //

function isConfigured() {
    if (settingsCache.api_format === FORMAT_GGUF) return Boolean(settingsCache.gguf_model.trim());
    return Boolean(settingsCache.api_url.trim() && settingsCache.model.trim() && settingsCache.api_key.trim());
}

function clearStatusTimer(node) {
    if (node?.__llmwTimer) clearInterval(node.__llmwTimer);
    node.__llmwTimer = null;
}

function setStatus(node, state) {
    const status = node?.__llmwStatus;
    if (!status) return;
    clearStatusTimer(node);
    if (state !== "loading") {
        status.textContent = "";
        status.classList.remove("is-loading");
        return;
    }
    status.classList.add("is-loading");
    const startedAt = globalThis.performance?.now?.() || Date.now();
    const update = () => {
        const elapsed = Math.max(0, Math.floor(((globalThis.performance?.now?.() || Date.now()) - startedAt) / 1000));
        status.textContent = elapsed > 1 ? `${TEXT.running} · ${elapsed}s` : `${TEXT.running}...`;
    };
    update();
    node.__llmwTimer = setInterval(update, 1000);
}

function syncNode(node) {
    const runButton = node?.__llmwRun;
    if (!runButton) return;
    const pending = Boolean(node.__llmwPending);
    runButton.textContent = pending ? "■" : "✦";
    runButton.title = pending ? TEXT.stop : TEXT.run;
    runButton.setAttribute("aria-label", runButton.title);
    runButton.classList.toggle("is-configured", isConfigured());
    runButton.classList.toggle("is-stop", pending);
    const clearButton = node.__llmwClear;
    if (clearButton) {
        // Nothing to clear outside continue mode: the answer field then holds a
        // single answer that the next run overwrites anyway.
        clearButton.hidden = !settingsCache.continue_chat;
        clearButton.disabled = pending;
    }
    // A log reads as a log in a monospaced face, and the field is still the
    // native textarea underneath — only its font is ours.
    widgetField(node, "answer")?.classList?.toggle("llmw-log", settingsCache.continue_chat);
    const unloadButton = node.__llmwUnload;
    if (unloadButton) {
        // Gemini holds nothing. The local backend frees its own cache, and an
        // OpenAI-compatible one is asked to — which only LM Studio answers, but
        // that is the local server people actually run out of VRAM with.
        const local = settingsCache.api_format === FORMAT_GGUF;
        unloadButton.hidden = settingsCache.api_format === FORMAT_GEMINI;
        unloadButton.disabled = pending;
        unloadButton.title = local ? TEXT.unload : TEXT.unloadHttp;
        unloadButton.setAttribute("aria-label", unloadButton.title);
    }
    syncMediaLine(node);
}

function syncAllNodes() {
    for (const node of targetNodes()) syncNode(node);
}

/**
 * Rebuild the chips under the toolbar.
 *
 * Called from onDraw, so it first compares a cheap signature: the upstream
 * loader's filename can change without any event this node can listen for, and
 * rebuilding the DOM on every frame would be wasteful.
 */
function syncMediaLine(node, { force = false } = {}) {
    const line = node?.__llmwMedia;
    if (!line) return;
    const resources = mediaResources(node);
    const signature = JSON.stringify([settingsCache.read_media, resources.map((item) => [item.type, item.name, Boolean(item.asset)])]);
    if (!force && signature === node.__llmwMediaSignature) return;
    node.__llmwMediaSignature = signature;
    line.textContent = "";
    if (!resources.length || !settingsCache.read_media) {
        line.hidden = true;
        return;
    }
    line.hidden = false;
    for (const resource of resources) {
        const chip = document.createElement("span");
        chip.className = `llmw-chip${resource.asset ? "" : " is-missing"}`;
        chip.textContent = `${resource.icon} ${resource.name}`;
        chip.title = resource.asset
            ? `${resource.tag}: ${[resource.asset.subfolder, resource.asset.filename].filter(Boolean).join("/")} (${resource.asset.storage})`
            : `${resource.tag}: ${TEXT.mediaNoFile}`;
        line.append(chip);
    }
}

function makeToolbarButton(className, label, title, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `llmw-tool ${className}`;
    button.textContent = label;
    button.title = title;
    button.setAttribute("aria-label", title);
    button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        onClick();
    });
    // Without this the canvas starts a node drag as soon as the button is
    // pressed and the click never reaches the handler.
    button.addEventListener("pointerdown", (event) => event.stopPropagation());
    return button;
}

function installToolbar(node) {
    if (typeof document === "undefined" || typeof node.addDOMWidget !== "function") return;

    const wrap = document.createElement("div");
    wrap.className = "llmw-toolbar";
    const bar = document.createElement("div");
    bar.className = "llmw-toolbar-row";
    const runButton = makeToolbarButton("is-run", "✦", TEXT.run, () => {
        if (node.__llmwPending) cancelGeneration(node);
        else generate(node);
    });
    const status = document.createElement("span");
    status.className = "llmw-status";
    const copyButton = makeToolbarButton("is-copy", "⧉", TEXT.copy, () => {
        // The reply, not the log around it — the whole conversation is one
        // select-all away in the text box itself.
        const answer = lastAnswer(widgetText(node, "answer"));
        if (!answer) return;
        navigator.clipboard?.writeText?.(answer).then(() => notify(TEXT.copied, "success")).catch(() => {});
    });
    const clearButton = makeToolbarButton("is-clear", "⌫", TEXT.clear, () => clearConversation(node));
    const unloadButton = makeToolbarButton("is-unload", "⏏", TEXT.unload, () => unloadModel(node));
    const settingsButton = makeToolbarButton("is-settings", "⚙", TEXT.settings, () => openSettings());
    const media = document.createElement("div");
    media.className = "llmw-media";
    media.hidden = true;
    // One button wide, so ✦ is not something the hand reaches by accident on
    // the way to ⏏ or ⧉. It sits before the run button rather than after a
    // particular neighbour, because ⏏ is hidden for Gemini.
    const gap = document.createElement("span");
    gap.className = "llmw-gap";

    // The status stretches so the buttons stay together in the bottom-right
    // corner of the node, with the run button in the corner itself.
    bar.append(status, settingsButton, clearButton, copyButton, unloadButton, gap, runButton);
    wrap.append(media, bar);
    // The canvas would otherwise zoom while the pointer sits over the toolbar.
    wrap.addEventListener("wheel", (event) => {
        event.preventDefault();
        event.stopPropagation();
        app.canvas?.processMouseWheel?.(event);
    }, { passive: false });

    const widget = node.addDOMWidget("llmw_toolbar", "llmw_toolbar", wrap, {
        serialize: false,
        margin: 4,
        getMinHeight: () => (media.hidden ? 30 : 52),
        getValue: () => "",
        setValue: () => {},
        onDraw: () => syncMediaLine(node),
    });
    if (!widget) {
        wrap.remove();
        return;
    }
    widget.serialize = false;
    node.__llmwRun = runButton;
    node.__llmwClear = clearButton;
    node.__llmwUnload = unloadButton;
    node.__llmwStatus = status;
    node.__llmwMedia = media;
    node.__llmwToolbar = widget;
    // Added last and left there, which puts it under the question field — the
    // run and copy buttons end up in the node's bottom-right corner, where the
    // eye already is after typing.
    syncNode(node);
}

// --------------------------------------------------------------------------- //
// Generation
// --------------------------------------------------------------------------- //

async function generate(node) {
    if (!node || node.__llmwPending) return;
    try {
        await loadSettings();
    } catch (error) {
        notify(error?.message || TEXT.loadFailed);
        return;
    }
    if (!isConfigured()) {
        notify(settingsCache.api_format === FORMAT_GGUF ? TEXT.missingGguf : TEXT.missingHttp, "warn");
        openSettings();
        return;
    }
    const question = widgetText(node, "question").trim();
    if (!question) {
        notify(TEXT.emptyQuestion, "warn");
        return;
    }
    const resources = mediaResources(node).map(({ type, tag, asset }) => ({ type, tag, asset }));
    const requestId = `${node.id}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    node.__llmwPending = true;
    node.__llmwRequestId = requestId;
    node.__llmwAbort = controller;
    setStatus(node, "loading");
    syncNode(node);
    try {
        const response = await api.fetchApi(GENERATE_ENDPOINT, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            signal: controller?.signal,
            body: JSON.stringify({
                request_id: requestId,
                system_prompt: widgetText(node, "system_prompt"),
                question,
                // Sent whatever the mode: the server decides from its own
                // settings whether the log is a conversation or just old text.
                transcript: widgetText(node, "answer"),
                resources,
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (data?.cancelled) throw new DOMException("cancelled", "AbortError");
        if (!response.ok || !data?.ok) throw new Error(data?.error || `HTTP ${response.status}`);
        // `transcript` is only returned in continue mode, and it already has
        // this turn appended.
        const transcript = String(data.transcript || "");
        setWidgetText(node, "answer", transcript || String(data.answer || ""));
        if (transcript) {
            // The question now lives in the log, so the box is free for the next
            // one — the way a chat input clears itself once the message is sent.
            setWidgetText(node, "question", "");
            // The newest turn is at the bottom, which is not where a textarea
            // that has just been refilled is looking.
            const field = widgetField(node, "answer");
            if (field) field.scrollTop = field.scrollHeight;
        }
        if (Array.isArray(data.skipped) && data.skipped.length) {
            notify(TEXT.mediaSkipped + data.skipped.join(", "), "warn");
        }
    } catch (error) {
        if (error?.name !== "AbortError") notify(error?.message || error);
    } finally {
        node.__llmwPending = false;
        node.__llmwRequestId = "";
        node.__llmwAbort = null;
        setStatus(node, "idle");
        syncNode(node);
    }
}

/**
 * Free whatever model the configured backend is holding.
 *
 * A GGUF is cached across runs so a second question does not reload it, and with
 * "Unload the model after answering" off nothing ever frees it. An
 * OpenAI-compatible server is asked in whichever way it understands — the server
 * works out whether it is talking to LM Studio or Ollama. Pointless against a
 * cloud endpoint, but those are not the ones people run out of VRAM with. Either
 * way this is the explicit way to hand the VRAM back before queueing the graph.
 */
async function unloadModel(node) {
    const button = node?.__llmwUnload;
    if (!button || button.disabled) return;
    button.disabled = true;
    try {
        const response = await api.fetchApi(UNLOAD_ENDPOINT, { method: "POST" });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data?.ok) throw new Error(data?.error || `HTTP ${response.status}`);
        // `detail` carries the server's own wording, e.g. which models it does
        // have resident when the configured one is not among them.
        const message = data.unloaded ? TEXT.unloaded : String(data.detail || TEXT.unloadIdle);
        notify(message, data.unloaded ? "success" : "info");
    } catch (error) {
        notify(error?.message || error);
    } finally {
        // syncNode owns the disabled state while a generation is pending.
        button.disabled = Boolean(node.__llmwPending);
    }
}

/**
 * Empty the log so the next question starts a new conversation.
 *
 * There is no history anywhere else — what the answer field holds *is* the
 * conversation — so this asks first.
 */
function clearConversation(node) {
    const button = node?.__llmwClear;
    if (!button || button.disabled) return;
    if (!widgetText(node, "answer").trim()) return;
    const ask = globalThis.confirm;
    if (typeof ask === "function" && !ask.call(globalThis, TEXT.clearConfirm)) return;
    setWidgetText(node, "answer", "");
    notify(TEXT.cleared, "info");
}

function cancelGeneration(node) {
    if (!node?.__llmwPending) return;
    const requestId = node.__llmwRequestId;
    // Tell the server first: it stops a local GGUF mid-generation, and the
    // abort below only stops this browser from waiting.
    if (requestId) {
        api.fetchApi(CANCEL_ENDPOINT, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ request_id: requestId }),
        }).catch(() => {});
    }
    node.__llmwAbort?.abort?.();
    node.__llmwPending = false;
    node.__llmwAbort = null;
    node.__llmwRequestId = "";
    setStatus(node, "idle");
    syncNode(node);
    notify(TEXT.cancelled, "info");
}

// --------------------------------------------------------------------------- //
// Registration
// --------------------------------------------------------------------------- //

function installNode(nodeType, nodeData) {
    if (nodeData?.name !== NODE_NAME) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
        const result = onNodeCreated?.apply(this, arguments);
        installToolbar(this);
        loadSettings().catch(() => {});
        if (this.size?.[0] < 380) this.setSize?.([380, this.size[1]]);
        return result;
    };

    const onConnectionsChange = nodeType.prototype.onConnectionsChange;
    nodeType.prototype.onConnectionsChange = function () {
        const result = onConnectionsChange?.apply(this, arguments);
        syncMediaLine(this, { force: true });
        return result;
    };

    const onAfterGraphConfigured = nodeType.prototype.onAfterGraphConfigured;
    nodeType.prototype.onAfterGraphConfigured = function () {
        // Links only exist once the whole graph is loaded, so a workflow opened
        // from disk gets its chips here rather than at node creation.
        const result = onAfterGraphConfigured?.apply(this, arguments);
        // syncNode too: a workflow opened after the settings were already
        // fetched gets no other chance to pick up the continue-mode styling.
        syncNode(this);
        syncMediaLine(this, { force: true });
        return result;
    };

    const onRemoved = nodeType.prototype.onRemoved;
    nodeType.prototype.onRemoved = function () {
        clearStatusTimer(this);
        if (this.__llmwPending) cancelGeneration(this);
        return onRemoved?.apply(this, arguments);
    };
}

function installStyle() {
    if (document.getElementById("llmw-style")) return;
    const style = document.createElement("style");
    style.id = "llmw-style";
    style.textContent = `
      .llmw-toolbar { display: flex; flex-direction: column; gap: 4px; width: 100%; box-sizing: border-box; font-family: "Google Sans", "Segoe UI", system-ui, -apple-system, sans-serif; }
      .llmw-toolbar-row { display: flex; align-items: center; gap: 6px; width: 100%; min-height: 26px; }
      .llmw-tool {
        appearance: none; width: 26px; height: 26px; flex: 0 0 26px; padding: 0; border: 1px solid rgba(255,255,255,.12); border-radius: 7px;
        background: rgba(255,255,255,.05); color: rgba(227,227,227,.62); cursor: pointer; font: inherit; font-size: 14px; line-height: 1;
        transition: background .18s, border-color .18s, color .18s;
      }
      .llmw-tool:hover, .llmw-tool:focus-visible { border-color: rgba(168,199,250,.42); background: rgba(168,199,250,.14); color: #dce7fa; outline: none; }
      .llmw-tool[hidden] { display: none !important; }
      .llmw-tool:disabled { cursor: default; opacity: .4; }
      .llmw-tool:disabled:hover { border-color: rgba(255,255,255,.12); background: rgba(255,255,255,.05); color: rgba(227,227,227,.62); }
      .llmw-gap { width: 26px; height: 26px; flex: 0 0 26px; pointer-events: none; }
      .llmw-tool.is-run { font-size: 15px; }
      .llmw-tool.is-run.is-configured { color: #a8c7fa; border-color: rgba(168,199,250,.3); }
      .llmw-tool.is-run.is-stop { color: #f28b82; border-color: rgba(242,139,130,.45); background: rgba(242,139,130,.12); font-size: 11px; }
      .llmw-status { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: rgba(227,227,227,.55); font-size: 11px; }
      .llmw-status.is-loading { color: #a8c7fa; }
      .llmw-media { display: flex; flex-wrap: wrap; gap: 4px; }
      .llmw-media[hidden] { display: none !important; }
      .llmw-chip {
        max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 1px 7px; border: 1px solid rgba(255,255,255,.1);
        border-radius: 999px; background: rgba(255,255,255,.04); color: rgba(227,227,227,.6); font-size: 10px; line-height: 16px;
      }
      .llmw-chip.is-missing { border-color: rgba(242,139,130,.35); color: rgba(242,139,130,.85); text-decoration: line-through; }
      /* The answer field in continue mode. Only the face changes: it is still
         ComfyUI's own textarea, with its IME handling, resizing and undo. */
      textarea.llmw-log { font-family: ui-monospace, "Cascadia Mono", Consolas, "Noto Sans Mono", monospace; line-height: 1.45; }

      .llmw-overlay {
        --llmw-bg: #1c1e23; --llmw-surface: #26282e; --llmw-text: #e3e3e3; --llmw-muted: #a8adb8;
        --llmw-border: rgba(255,255,255,.12); --llmw-border-light: rgba(255,255,255,.18); --llmw-accent: #a8c7fa;
        position: fixed; inset: 0; z-index: 10090; display: flex; align-items: center; justify-content: center; padding: 16px;
        box-sizing: border-box; background: rgba(0,0,0,.58); color: var(--llmw-text);
        font-family: "Google Sans", "Segoe UI", system-ui, -apple-system, sans-serif;
      }
      .llmw-dialog {
        width: min(560px, calc(100vw - 32px)); max-height: calc(100vh - 32px); box-sizing: border-box;
        overflow-x: hidden; overflow-y: auto;
        border: 1px solid rgba(255,255,255,.15); border-radius: 16px; background: var(--llmw-bg); color: var(--llmw-text);
        box-shadow: 0 24px 64px rgba(0,0,0,.6), inset 0 1px 0 rgba(255,255,255,.05);
      }
      .llmw-header { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 14px 20px 12px; border-bottom: 1px solid var(--llmw-border); }
      .llmw-title { min-width: 0; font-size: 17px; font-weight: 600; }
      .llmw-header-actions { display: flex; align-items: center; gap: 2px; flex: 0 0 auto; }
      .llmw-close {
        appearance: none; width: 28px; height: 28px; flex: 0 0 28px; padding: 0; border: 1px solid transparent; border-radius: 8px; background: transparent;
        color: rgba(227,227,227,.56); cursor: pointer; font: inherit; font-size: 17px; line-height: 1; transition: background .2s, border-color .2s, color .2s;
      }
      .llmw-close:hover, .llmw-close:focus-visible { border-color: rgba(168,199,250,.32); background: rgba(168,199,250,.1); color: #dce7fa; outline: none; }
      /* minmax(0, 1fr) rather than the default: a grid item refuses to shrink
         below its content's min-content width, and a number input or a long
         model path is enough to push the dialog into a horizontal scrollbar. */
      .llmw-form { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; padding: 14px 20px 16px; }
      .llmw-row { display: flex; flex-direction: column; align-items: stretch; gap: 5px; min-width: 0; min-height: 0; }
      .llmw-row[hidden], .llmw-check[hidden], .llmw-hint[hidden] { display: none !important; }
      .llmw-label { color: var(--llmw-muted); font-size: 13px; font-weight: 500; }
      .llmw-hint { min-width: 0; margin: -2px 0 2px; color: var(--llmw-muted); font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
      .llmw-control {
        width: 100%; min-width: 0; box-sizing: border-box; height: 34px; padding: 7px 12px; border: 1px solid var(--llmw-border); border-radius: 8px;
        background: var(--llmw-bg); color: var(--llmw-text); outline: none; font: inherit; font-size: 14px; transition: border-color .2s, background .2s;
      }
      .llmw-control:hover { border-color: var(--llmw-border-light); }
      .llmw-control:focus, .llmw-control.is-open { border-color: var(--llmw-accent); background: #1a1b1e; }
      .llmw-select-wrap { position: relative; min-width: 0; width: 100%; user-select: none; }
      .llmw-select { display: flex; align-items: center; justify-content: space-between; gap: 10px; text-align: left; cursor: pointer; }
      .llmw-select-value { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .llmw-select-chevron { width: 8px; height: 8px; flex: 0 0 8px; margin: -4px 3px 0 0; border-right: 1.5px solid var(--llmw-muted); border-bottom: 1.5px solid var(--llmw-muted); transform: rotate(45deg); transition: transform .15s ease-out, border-color .15s ease-out; }
      .llmw-select:hover .llmw-select-chevron, .llmw-select.is-open .llmw-select-chevron { border-color: var(--llmw-text); }
      .llmw-select.is-open .llmw-select-chevron { transform: rotate(225deg) translate(-1px, -1px); }
      .llmw-select-menu {
        position: absolute; top: calc(100% + 6px); left: 0; right: 0; z-index: 100; max-height: 320px; overflow: auto; padding: 6px;
        border: 1px solid rgba(255,255,255,.08); border-radius: 12px; background: rgba(38,40,46,.97); box-shadow: 0 12px 32px rgba(0,0,0,.6);
      }
      .llmw-select-option {
        display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; min-height: 34px; padding: 8px 12px; border: 0; border-radius: 8px;
        background: transparent; color: var(--llmw-text); cursor: pointer; font: inherit; font-size: 13px; text-align: left; transition: background .12s, color .12s;
      }
      .llmw-select-option:hover { background: rgba(255,255,255,.06); }
      .llmw-select-option.is-selected { background: rgba(168,199,250,.1); color: var(--llmw-accent); font-weight: 500; }
      .llmw-select-option.is-selected::after { content: "\\2713"; margin-left: auto; }
      .llmw-check { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-width: 0; min-height: 26px; font-size: 13px; }
      .llmw-check > span { min-width: 0; overflow-wrap: anywhere; }
      .llmw-switch { position: relative; display: inline-block; width: 40px; height: 22px; flex: 0 0 40px; padding: 0; border: 0; background: transparent; cursor: pointer; }
      .llmw-switch-track { position: absolute; inset: 0; display: block; border: 1px solid rgba(255,255,255,.1); border-radius: 22px; background: #22252a; transition: background-color .2s, border-color .2s; }
      .llmw-switch-thumb { position: absolute; left: 3px; bottom: 3px; width: 16px; height: 16px; border-radius: 50%; background: #747a83; box-shadow: 0 1px 3px rgba(0,0,0,.32); transition: transform .2s, background-color .2s; }
      .llmw-switch.is-on .llmw-switch-track { border-color: rgba(255,255,255,.17); background: #30343a; }
      .llmw-switch.is-on .llmw-switch-thumb { background: var(--llmw-accent); transform: translateX(18px); }
      .llmw-switch:focus-visible { outline: 2px solid var(--llmw-accent); outline-offset: 3px; border-radius: 12px; }
      .llmw-error { margin: -4px 20px 12px; color: #f28b82; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; white-space: pre-wrap; }
      .llmw-button {
        appearance: none; height: 28px; padding: 0 9px; border: 1px solid transparent; border-radius: 8px; background: transparent;
        color: rgba(227,227,227,.56); cursor: pointer; font: inherit; font-size: 12px; font-weight: 500; transition: all .2s cubic-bezier(.2,0,0,1);
      }
      .llmw-button:hover, .llmw-button:focus-visible { border-color: rgba(168,199,250,.32); background: rgba(168,199,250,.1); color: #dce7fa; outline: none; }
      .llmw-button:disabled { cursor: wait; opacity: .52; }
    `;
    document.head.append(style);
}

app.registerExtension({
    name: "LLMWidget",
    setup() {
        installStyle();
        api.addEventListener?.(ANSWER_EVENT, (event) => {
            const detail = event?.detail || {};
            const node = app.graph?.getNodeById?.(Number(detail.node_id));
            if (isTarget(node)) setWidgetText(node, "answer", String(detail.answer || ""));
        });
    },
    beforeRegisterNodeDef(nodeType, nodeData) {
        installNode(nodeType, nodeData);
    },
});
