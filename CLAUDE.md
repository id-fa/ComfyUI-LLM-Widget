# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A ComfyUI custom-node pack (`ComfyUI/custom_nodes/ComfyUI-LLM-Widget`) with exactly one node,
`LLMWidget`. It asks an LLM a question with an optional connected image/video and stores the
answer on the node.

There is no build system, no test suite, no linter, and `dependencies = []` in `pyproject.toml`.
Two source files:

- `nodes.py` — settings file, URL normalization, the OpenAI/Gemini/GGUF/text-encoder backends, the media
  resolution, the HTTP routes, and the node class.
- `web/llm_widget_ui.js` — the toolbar DOM widget, the settings dialog, link tracing, and the
  fetch/cancel plumbing.

Derived from the prompt optimizer of `ComfyUI-MiniMaxH3-Easy` (MIT). When something here looks
under-specified, that pack is the reference implementation — but do **not** port its prompt-guide
machinery, virtual media links, or contenteditable prompt editor. Those were deliberately dropped.

## Development loop

- Python changes require a **full ComfyUI restart**.
- `web/*.js` changes require only a **browser hard refresh** (`WEB_DIRECTORY = "./web"`).
- There is nothing to run locally without a ComfyUI installation; verification is manual in the
  ComfyUI canvas.
- `llm_widget.json` (repo root, gitignored) holds a **plaintext API key**. Never read it into
  output, commit it, or include it in packaging.

## The central design point: it runs without executing the graph

The node's whole reason to exist is that pressing ✦ in the editor produces an answer immediately,
with nothing queued. That is why:

- the work lives on `PromptServer` routes under `/llm_widget/`, not in `LLMWidget.run`;
- media has to be resolved from **files**, because a tensor produced mid-graph does not exist yet;
- the node is expected to be permanently bypassed or muted in the user's workflow;
- `LLMWidget.run` normally just returns the stored `answer` widget value.

`generate_on_execute` is the escape hatch for headless/API runs: it runs the same
`_generate` pipeline during execution, where media comes from the real tensors via
`_execution_media_items` instead of file paths, and pushes the result back to the editor with
`PromptServer.send_sync(ANSWER_EVENT)`.

## Route access (`_request_problem`)

Versions 0.1.0–0.1.4 were **banned from the Comfy Registry** for `UNAUTHENTICATED_SIDE_EFFECT`:
`POST /llm_widget/settings` took `await request.json()` — which ignores `Content-Type`, so a
cross-origin `text/plain` "simple request" reaches it with no preflight — and wrote the config.
That is a real exploit, not scanner noise: repoint `api_url` and the next ✦ sends the API key, the
question and the images to the attacker. The reviewer named the three missing gates (no server-minted
token / non-simple header, no Host allowlist, no authentication), and every route now goes through
`refused()` → `_request_problem`:

- same origin: `Sec-Fetch-Site: cross-site` and an `Origin` that does not match `Host` are refused.
  Core has a middleware like this, but only for loopback hosts and not at all under
  `--enable-cors-header`, so it is not relied on;
- `TOKEN_HEADER` must carry `_ROUTE_TOKEN`, minted per process and handed out only by
  `GET /settings` (the one route checked with `token=False`). The JS sends everything through
  `callRoute`, which refetches the settings and retries once on a 403, because the token dies with
  the server while the page lives on. **A new route must call `refused(request)` first, and a new
  JS call must use `callRoute`, not `api.fetchApi`;**
- a connection from loopback must have an IP literal or `localhost` as its `Host`. A DNS name there
  is a rebinding page, which is same-origin with itself and can read the token, so neither other
  check stops it. `allowed_hosts` in `llm_widget.json` is the escape hatch for a same-machine
  reverse proxy, and `_update_config` deliberately never takes it from the request.

**The API key never goes back to the editor**: `_public_config` blanks it and adds `api_key_set`;
the dialog sends `api_key_keep` when the field was not touched and `_update_config` keeps the stored
key. `isConfigured` therefore reads `api_key_set`, never `api_key`.

What is left in the registry scan is YARA's `python_network_operations` on `urllib.request.urlopen`
(severity info). It is the node's function — calling the LLM endpoint the user configured — and is
not to be "fixed" by hiding the call behind `getattr`, `importlib` or string concatenation: that is
what malware does, and a human reviewer reads it that way. `tools/` is kept out of the registry
package by `.comfyignore` because `install_helper.py` trips three more rules by design
(`subprocess`, `os.environ`, GitHub token).

## Media resolution (split across both files)

1. JS `inputSourceNode` follows the `image` / `video` input link, walking through any node whose
   type matches `/reroute/i`, to the node that actually holds a filename widget.
2. JS `sourceAsset` scans that node's widgets for a filename, preferring the conventional names
   per modality, and splits an inline `photo.png [output]` annotation into `filename` + `storage`.
3. The `{filename, subfolder, storage}` descriptor is POSTed as `resources`.
4. Python `_asset_path` re-resolves it under `input/`/`output/`/`temp/` **with a realpath prefix
   check** — keep that check when touching this. A path that escapes those roots returns `None`.

There are `IMAGE_INPUT_MAX` image sockets — `image`, `image2` … `image10`; the first keeps its old
name so workflows saved with one socket still connect — and one `video`. **A tag is the socket
number, not the rank among the connected ones**: with `image2` empty, `image3` is still sent as
`image 3`, because multi-reference edit models (Qwen-Image) address their inputs by number and the
prompt being written has to use the same numbers. The JS `MEDIA_INPUTS` table and Python
`_execution_media_items` both tag this way; `_media_items` only falls back to counting when a
resource arrives without a tag. The chips are one row that scrolls sideways, like the tab strip,
so eleven of them do not change the toolbar height.

An unresolvable media is *kept* as an item with empty `parts` so the run can report what it
skipped (`skipped` in the response, red struck-through chip in the toolbar) rather than silently
answering about nothing. Do not "simplify" that by dropping the item.

## Video sampling

A chat message has no video channel, so a video becomes stills: `_video_still_parts` from a file,
`_tensor_video_parts` (through `_sample_frames`) from the tensors an execution receives. Both take
candidates at `VIDEO_SAMPLE_RATE` fps, then spend the `video_sample` budget with
`_select_change_frames` — first frame, last frame, and the rest on the candidates that differ most
from the one before them (`_still_change_scores` on a `VIDEO_SAMPLE_SCORE_SIDE` grayscale
thumbnail, `_tensor_change_scores` by striding the tensor). Evenly spaced stills kept sending a
held shot several times over while the cut in the middle went unseen; that is what this replaces.
The candidate set itself is capped at `VIDEO_SAMPLE_MAX_CANDIDATES`, because 1 fps over a long clip
decodes a few hundred frames only to throw most of them away.

**Because that selection is uneven, every path that sends it also states the timestamps**
(`_video_sample_detail`). Both samplers therefore return `(parts, times, duration)`, the times ride
on the media item (`times` / `duration`) and reach the model through `_media_manifest` and
`VIDEO_STILLS_REQUEST`. Dropping them is not cosmetic: `0.0s / 5.0s / 6.0s / 10.0s` read as four
equal steps turns a held shot and a cut into a slow continuous move. When the times are unknown the
wording falls back to `VIDEO_SAMPLE_ORDER` rather than inventing any.

An IMAGE *batch* is not a clip — it has no timeline to reason about — so `_tensor_still_parts`
keeps thinning it evenly. Gemini never uses any of this: it takes the file whole.

## Backends

`_generate` is the single entry point shared by the route and by execution. It takes
already-resolved `media_items`, so neither caller knows about the other's media source.

- **openai / gemini** (`_http_generate`) — one `urllib` POST. URL handling is forgiving
  (`_normalize_url` appends/strips endpoints, injects the Gemini model path). `_remote_unload`
  is the one other thing spoken to an OpenAI-compatible server, and it is *not* an
  OpenAI-compatible route: it is LM Studio's own REST API (`/api/v1/models/unload`) or Ollama's
  `keep_alive: 0` on `/api/generate`, both rooted by `_server_root`, which is why the `/v1` has
  to come off. Which server it is comes from the listing probes (`/api/v1/models` vs
  `/api/ps`) — neither has the other's route, so the one that answers is the answer. That
  detection is deliberate: **do not split `api_format` into per-vendor options.** They all
  generate through the same endpoint, and one button is not worth tripling the settings, the
  validation and the format rows in the dialog. If a server ever answers both probes, add an
  override *inside* the openai format instead.
- **gguf** (`_gguf_generate`) — `llama-cpp-python`. The loaded `Llama` is cached in `_GGUF_STATE`
  keyed by `(model, mmproj, ctx, gpu_layers)` and released when that changes, on
  `gguf_unload_after`, on cancel, or from the toolbar's ⏏ (`POST /llm_widget/unload`). Vision
  needs an mmproj plus a chat handler class whose name varies per llama-cpp build/fork, so
  `_gguf_chat_handler` probes candidates by model-name family and degrades to text-only.
  `_generate` holds `_gguf_hold` for the whole local run so the unload route can refuse while a
  worker thread is generating — closing that `Llama` mid-generation takes llama-cpp down with it.
  Every other `_gguf_release` call site already runs after the run unwound.
- llama-cpp takes the same OpenAI-shaped `image_url` parts, so the media builders are shared and
  the GGUF path passes `FORMAT_OPENAI` as its `parts_format`.

- **clip** (`_clip_generate`) — a text encoder in ComfyUI's safetensors format run as the LLM
  through comfy's own `comfy.sd.load_clip` → `clip.tokenize` → `clip.generate` → `clip.decode`,
  the path behind core's `Generate Text` node (`comfy_extras/nodes_textgen.py` is the reference).
  It exists because the encoder of Qwen-Image 2.1 *is* Qwen3-VL-8B. Things that are not obvious:
  - **No `clip_type` is passed to `load_clip`.** A type selects an image model's conditioning
    variant of the encoder; the untyped default is the generic wrapper, and that is the one built
    to generate.
  - **The prompt is a hand-built ChatML string** for the families in `_CLIP_CHATML_FAMILIES`
    (matched on `clip.tokenizer.clip_name`). comfy's Qwen tokenizers skip their single-turn
    template for a text that starts with `<|im_start|>` and replace each `<|image_pad|>` with the
    next entry of `images=[…]`, which is the only way a system prompt, the history and images of
    different sizes get in. The dict value is the closed `<think>` block that family reads as
    thinking-off — the spacing differs between Qwen3-VL and Qwen3.5 and is copied from their
    tokenizers. Any other family falls back to comfy's template: everything folded into the user
    text, images resized to one size because `image=` is a single batch.
  - The media pipeline is not forked: the backend takes the same OpenAI-shaped `parts`
    (`LOCAL_FORMATS` → `FORMAT_OPENAI`) and `_clip_image_tensors` decodes them back, capped at
    `CLIP_IMAGE_MAX_SIDE` because comfy's Qwen preprocessor would keep up to 12.8 MP.
  - **Every `generate()` is followed by `_clip_after_generate()`**, which is
    `comfy.model_prefetch.cleanup_prefetch_queues()` — what `execution.py` runs in the `finally`
    of each node. A Qwen3 encoder (`fixed_kv` / `graph_dynamic_vbar_blocks` in its config) decodes
    through per-layer CUDA graphs captured on the first decode step and replayed whenever the
    layer's weights are still in the same VRAM block; the KV cache and position tensors are not
    part of that check, so the *second* `generate()` on a loaded model replays graphs writing into
    the first call's freed KV cache: `ScatterGatherKernel.cu … index out of bounds`, a sticky CUDA
    error, `Fatal Python error: Aborted`. Core never sees it because `TextGenerate` generates once
    per node and the executor cleans up after it; here the model stays loaded between ✦ presses
    and the editor route is not a node, so the second ✦ was the crash. Do not remove the call to
    make a run faster. (Found in ComfyUI-MiniMaxH3-Easy, whose describe pass hit it inside one
    request; the fix is the same.)
  - **`generate` takes no stop callback**, and `comfy.model_management.InterruptProcessingException`
    is a `BaseException`, so the route's `except Exception` never sees it. The editor route
    therefore (a) clears a stale interrupt flag before starting — ComfyUI's Cancel button raises it
    even with nothing running and it stays up until the next workflow; (b) stops a run through that
    same flag: the cancel route calls `_clip_interrupt`, which raises it only while
    `_CLIP_ACTIVE_REQUEST` is the cancelled id and no workflow is executing, and `_clip_generate`
    turns the resulting exception into `_Cancelled` when `should_stop()` agrees and into a visible
    error otherwise. Neither happens with `executing=True`: there the flag belongs to the executor
    and the exception is re-raised for it. `_clip_cancel_hook` (the `PROGRESS_BAR_HOOK` swap,
    polling the cancel registry *on the owning thread only* and forwarding every other thread to
    the server's hook) is still installed but is only a backstop: `ProgressBar` throttles the hook
    to 0.5 % of the budget, which with a large `max_length` is dozens of tokens.
  - **The route refuses while a workflow is running** (`_clip_workflow_running`): comfy's model
    management has no lock, and `load_models_gpu` from a worker thread next to the executor races
    over the same bookkeeping. `executing=True` from `LLMWidget.run` skips the check, because
    there the caller *is* the executor. Both callers wrap the run in `torch.inference_mode()`, as
    the executor does, so the cached model is usable from either.
  - `_CLIP_STATE` / `_clip_hold` / `_clip_unload_now` mirror the GGUF trio. Release goes through
    `unload_model_and_clones`; dropping the reference alone leaves the weights on the GPU.
  - There is no describe mode and no mmproj here; do not port them.

`gguf_describe_media` switches to a two-stage shape: `_gguf_describe` runs one small vision pass
per media, then `_gguf_generate` gets the descriptions as `context` and no images. It exists
because a large multimodal prompt makes one uninterruptible prompt-evaluation phase — the reason
the editor appears to freeze. Because that pass loads the projector, the final text-only pass takes
`keep_vision=True`; without it the signature would change and the model would be reloaded between
the two.

Three things that mode gets wrong if you are not careful, found with Gemma 4 and Qwen3.8:

- **The describe pass has its own token budget, and a model that cannot be told to stop reasoning
  spends it on the thought.** `DESCRIBE_LENGTH` alone left Gemma stopping mid-thought, so
  `_clean_output` correctly returned nothing at all and *every* description came back empty.
  `DESCRIBE_THINKING_HEADROOM` is added for the families with no working switch (gemma), and is
  deliberately not tied to `max_length`: it buys room for text that is discarded either way.
- **A vision chat handler renders no chat template**, so neither switch reaches the model on the
  describe path and the budget is the only lever left. `_gguf_chat` raises `_ThinkingOverflow` for
  exactly that failure, and `_gguf_describe` retries the media with `DESCRIBE_THINKING_HEADROOM` on
  top and keeps the raised budget for the rest of the run — one wasted pass, not one lost
  description per media.
- **A describe pass that produced nothing must not be treated as "no media was connected".**
  `_generate` falls back to the single-prompt path (`describe = False`) and logs it. Without that
  the final pass got no parts *and* no media rule, and the model answered — reasonably — that
  there was nothing to look at. Same principle as the empty-`parts` item in `_media_items`: never
  answer about media as if it had not been there.

## Continue mode

`continue_chat` makes the run replay the earlier turns. There is **no separate history store**:
the `answer` widget *is* the conversation, written as an IRC-style log with the line-start
markers `CHAT_USER_MARK` / `CHAT_MODEL_MARK`. That is the whole point — the log is a plain
textarea, so editing or deleting lines edits what the model remembers, and it is saved with the
workflow like any other widget value. Do not move it into a hidden serialized widget.

- `_parse_transcript` → `_history_messages` (merges consecutive same-role turns, keeps the last
  `history_turns` exchanges, drops a leading assistant turn) → the backends' `messages` /
  `contents`. Gemini rejects two user turns in a row, which is why the merge is not optional.
- Text before the first marker is **not** part of the conversation. It is whatever the widget
  held when the mode was switched on, and it is left alone rather than deleted.
- `_append_turn` and `_last_answer` live in Python and are the only writers/readers of the
  format; the route returns `transcript` (the whole log) *and* `answer` (this reply). The JS
  copy of the rule is `lastAnswer`, used only for the ⧉ button.
- `chat_blank_lines` only changes what `_append_turn` writes between blocks. A turn ends where
  the next marker begins, so both spacings parse the same and a log written under one setting
  keeps working under the other — do not make the parser depend on it.
- Earlier turns are **text only** on every backend. Re-encoding an image on every following turn
  costs more than it is worth and its file may be gone; media attaches to the current question.
- The node's `text` output is always `_last_answer`, never the log — this is why the widget can
  hold a whole conversation without breaking anything wired downstream.
- The editor clears the `question` widget after a successful continue-mode turn, because the
  question is in the log by then.

## Cancellation

`✦` becomes `■` while pending, aborting the fetch and calling `POST /llm_widget/generate_cancel`
with the request id the frontend generated. The id registry (`_cancel` / `_is_cancelled`, capped
at `CANCEL_LIMIT`) is polled by `_gguf_stream` between tokens.

**`create_chat_completion` has no `stopping_criteria`** (only `create_completion` does), so
streaming and closing the generator is the only way to interrupt a chat turn; model loading and
prompt evaluation still cannot be interrupted at all. urllib cannot be interrupted either, so an
HTTP answer that arrives after a cancel is discarded instead. A client disconnect raises
`asyncio.CancelledError` in the route and is treated as a cancel — but the model is *not* freed
from that handler, because the worker thread outlives it and does that itself.

## Reasoning

The `thinking` setting is `off` (default) or `keep`; `off` means *both* "send the switches" and
"strip what comes back". Three mistakes were made here already — do not reintroduce any of them:

1. **`_http_generate` originally sent nothing at all.** Only the GGUF path had `/no_think`, so any
   OpenAI-compatible server reasoned freely into the answer. The switches now go through
   `_thinking_off_payload`, and `_http_post` retries once **without** them on 400/404/422, because
   an endpoint that has never heard of `chat_template_kwargs` answers with an error rather than
   ignoring it. `_gguf_call` does the same for llama-cpp builds too old to accept the argument.
   Qwen3.8 reads the *depth* rather than the switch and defaults to `xhigh`, hence
   `REASONING_EFFORT = "low"`; both fields go in the same dict because older templates read only
   `enable_thinking`, and `none` is not a value that template accepts.
2. **`_clean_output` required an opening `<think>`.** Most Qwen chat templates *pre-open* the tag
   in the assistant turn, so the response begins with bare reasoning prose and the only tag in it
   is the closing one — the whole block leaked. `_THINK_CLOSE_RE` therefore makes the opening tag
   optional and is greedy to the **last** closing tag.
3. **Only Harmony's exact `<|channel|>final<|message|>` was recognised.** Gemma 4 marks its
   reasoning with the same idea but different pipes — `<|channel>thought` … `<channel|>`, the
   closing marker carrying no role and no `<|message|>` — so the whole thought leaked.
   `_CHANNEL_MARK` therefore accepts every spelling, and `_THOUGHT_CHANNEL_RE` cuts to the last
   marker when the block opens on a thinking role. Note that `<|channel>` is *also* in the gemma
   `stop` list in `_gguf_chat`; it evidently does not fire through the vision chat handler, but
   do not add the closing marker there — the answer is what follows it.

Untagged reasoning cannot be removed: nothing marks where it ends. Do not add heuristics that
guess at prose preambles — they will eat real answers. The switches are what has to work; the
honest fallback is telling the user to pick a non-thinking model.

A block that never closes means the model ran out of tokens while thinking, and `_clean_output`
returns "" because there genuinely is no answer in it. `_gguf_chat` turns that specific case into
an error that says so (`_opens_with_thinking`) instead of an unexplained empty answer: the user
can act on "raise Max answer tokens", not on "empty".

`_clean_output` unwraps a whole-answer code fence **only** for languages in `_PLAIN_FENCE_LANGS`.
A ` ```python ` block is the answer itself; stripping it would corrupt a legitimate request for
code. This is a deliberate divergence from the MiniMax H3 optimizer, which always strips fences.

## Text fields stay native

`system_prompt`, `answer` and `question` are ordinary ComfyUI multiline `STRING` widgets. They are
**not** replaced by DOM widgets: the native textarea already handles IME composition (this pack's
author writes Japanese), resizing and undo. The DOM widgets are the system prompt tab strip (see
below) and the toolbar, which is added last
and stays last, so the run and copy buttons sit in the node's bottom-right corner where the eye
already is after typing. Inside the row the status label stretches and the buttons are
right-aligned, with an empty button's width (`.llmw-gap`) held open in front of ✦ so a misaimed
click lands on nothing instead of ⏏; the media chips sit above the button row.

**The order of the `INPUT_TYPES` dict *is* the layout**, and it is
`system_prompt → generate_on_execute → answer → question`. The question box therefore sits at the
bottom with the toolbar under it: a chat input below its own transcript, with ✦ where a send button
belongs. `generate_on_execute` is a setting rather than part of that flow, which is why it is above
them instead of between the question and the button. Do not make the order depend on
`continue_chat`, however tempting: `widgets_values` is
serialized by index, so a workflow saved under one setting would load with `question` and `answer`
swapped under the other — and the setting is installation-global. Reordering `node.widgets` from
the frontend has the same defect plus a frontend-version one.

`setWidgetText` writes `widget.value`, `widget.element.value` and `widget.inputEl.value` and calls
the callback, because which of those a multiline widget actually reads differs across ComfyUI
frontend versions.

## System prompt tabs

Ported from `ComfyUI-PromptPalette-F`'s Prompt Tabs by way of the MiniMax H3 Easy port, and
frontend-only — `nodes.py` knows nothing about it:

```
node.properties["llmw_system_tabs"]      = [{ label, text }, …]
node.properties["llmw_system_tab_index"] = open tab
```

- **The `system_prompt` widget is the editor of the open tab and the authority for its text.** It
  is what the route and `run` receive, so only the open tab is ever sent, and a workflow works the
  same with the extension disabled. Nothing listens to keystrokes: `flushSystemTab` copies the
  widget into the open tab right before anything reads the tabs (a tab operation, `onSerialize`,
  `onConfigure`). On load the widget wins over the stored tab text for the same reason.
- It is `node.properties` and not a hidden `tabs_data` widget (PromptPalette's shape) because a
  widget would be one more index in `widgets_values`.
- `systemTabs()` is the only accessor and migrates on first touch: a node with no tabs gets one
  holding whatever `system_prompt` has.
- **The strip is the one widget spliced out of creation order** — it has to sit above
  `system_prompt`, i.e. at index 0 — and frontends disagree on whether a `serialize = false`
  widget holds an index in `widgets_values` (LiteGraph wrote holes and read by index; newer
  frontends skip it on one side or both). So `saveNodeWidgets` (`onSerialize`) rewrites
  `widgets_values` as the four `SAVED_WIDGETS` in `INPUT_TYPES` order, exactly what a strip-less
  node writes, and `restoreNodeWidgets` (`onConfigure`) reassigns them by name. `SAVED_WIDGETS`
  must follow `INPUT_TYPES`. `onSerialize` runs *after* LiteGraph cloned the properties, which is
  why the tabs are written into `data.properties` too.
- The strip is one fixed-height row that scrolls sideways (`getMinHeight` = `getMaxHeight`), so
  there is no wrapped-height tracking and no ResizeObserver.
- PromptPalette's Nodes 2.0 grid-row pinning *was* ported (`applyRowSizing`): the Vue node body is
  a grid whose `align-content: stretch` shares the spare height among all `auto` rows, which
  inflated the strip and the toolbar. Rows without a textarea become `min-content`; the text
  fields stay `auto`, never a pixel height, or the node stops being shrinkable. Two
  MutationObservers keep it applied — one on the grid's `style` (the frontend rewrites
  `grid-template-rows`), one `childList` on the node root (a remount replaces the grid and strands
  the first). Both are dropped in `onRemoved` (`releaseRowSizing`), and `applyRowSizing` refuses a
  node with no `graph` so a late frame cannot latch a removed node onto the element of a reloaded
  one with the same id. No-op in the classic renderer.
- Renaming is an inline input, not `window.prompt`; Enter is ignored while `isComposing`, because
  it also confirms an IME conversion. A strip rebuild commits a rename in progress
  (`node.__llmwTabCommit`) since not every browser blurs a removed element.

## Cross-file invariants

`SAVED_WIDGETS` in the JS must list the widgets of `INPUT_TYPES` in the same order.

Constants duplicated between `nodes.py` and `web/llm_widget_ui.js` must be edited in both: the
route paths under `/llm_widget/`, `ANSWER_EVENT`, `TOKEN_HEADER`, the four format ids and `LOCAL_FORMATS`, `GGUF_MMPROJ_AUTO` /
`GGUF_MMPROJ_NONE`, `CHAT_USER_MARK` / `CHAT_MODEL_MARK`, the `VIDEO_SAMPLES` ids, `IMAGE_INPUT_MAX` with the socket
naming (`image`, `image2`, …), and every
settings key with its clamp range (both sides normalize independently, and the server's
normalization is authoritative).

## Conventions

- All user-visible UI text is English, in the `TEXT` table in the JS. There is no localization
  layer — MiniMax H3 Easy's homemade EN/ZH tables were deliberately not carried over.
- Python log lines go through `_log` / `LOG_PREFIX` so a run that takes minutes shows progress.
- `IS_CHANGED` returns `NaN` so an edited answer always propagates.
- Commit messages are short imperative one-liners.
