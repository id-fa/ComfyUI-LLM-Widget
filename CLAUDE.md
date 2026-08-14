# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A ComfyUI custom-node pack (`ComfyUI/custom_nodes/ComfyUI-LLM-Widget`) with exactly one node,
`LLMWidget`. It asks an LLM a question with an optional connected image/video and stores the
answer on the node.

There is no build system, no test suite, no linter, and `dependencies = []` in `pyproject.toml`.
Two source files:

- `nodes.py` — settings file, URL normalization, the OpenAI/Gemini/GGUF backends, the media
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

## Media resolution (split across both files)

1. JS `inputSourceNode` follows the `image` / `video` input link, walking through any node whose
   type matches `/reroute/i`, to the node that actually holds a filename widget.
2. JS `sourceAsset` scans that node's widgets for a filename, preferring the conventional names
   per modality, and splits an inline `photo.png [output]` annotation into `filename` + `storage`.
3. The `{filename, subfolder, storage}` descriptor is POSTed as `resources`.
4. Python `_asset_path` re-resolves it under `input/`/`output/`/`temp/` **with a realpath prefix
   check** — keep that check when touching this. A path that escapes those roots returns `None`.

An unresolvable media is *kept* as an item with empty `parts` so the run can report what it
skipped (`skipped` in the response, red struck-through chip in the toolbar) rather than silently
answering about nothing. Do not "simplify" that by dropping the item.

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

`gguf_describe_media` switches to a two-stage shape: `_gguf_describe` runs one small vision pass
per media, then `_gguf_generate` gets the descriptions as `context` and no images. It exists
because a large multimodal prompt makes one uninterruptible prompt-evaluation phase — the reason
the editor appears to freeze. Because that pass loads the projector, the final text-only pass takes
`keep_vision=True`; without it the signature would change and the model would be reloaded between
the two.

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

`_clean_output` unwraps a whole-answer code fence **only** for languages in `_PLAIN_FENCE_LANGS`.
A ` ```python ` block is the answer itself; stripping it would corrupt a legitimate request for
code. This is a deliberate divergence from the MiniMax H3 optimizer, which always strips fences.

## Text fields stay native

`system_prompt`, `answer` and `question` are ordinary ComfyUI multiline `STRING` widgets. They are
**not** replaced by DOM widgets: the native textarea already handles IME composition (this pack's
author writes Japanese), resizing and undo. The only DOM widget is the toolbar, which is added last
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

## Cross-file invariants

Constants duplicated between `nodes.py` and `web/llm_widget_ui.js` must be edited in both: the
route paths under `/llm_widget/`, `ANSWER_EVENT`, the three format ids, `GGUF_MMPROJ_AUTO` /
`GGUF_MMPROJ_NONE`, `CHAT_USER_MARK` / `CHAT_MODEL_MARK`, and every settings key with its clamp
range (both sides normalize independently, and the server's normalization is authoritative).

## Conventions

- All user-visible UI text is English, in the `TEXT` table in the JS. There is no localization
  layer — MiniMax H3 Easy's homemade EN/ZH tables were deliberately not carried over.
- Python log lines go through `_log` / `LOG_PREFIX` so a run that takes minutes shows progress.
- `IS_CHANGED` returns `NaN` so an edited answer always propagates.
- Commit messages are short imperative one-liners.
