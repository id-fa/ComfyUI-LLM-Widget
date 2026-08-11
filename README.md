# ComfyUI-LLM-Widget

A single ComfyUI node that asks an LLM a question — optionally about a connected image or
video — and keeps the answer on the node.

It runs **from the editor**, before and independently of any graph execution. Press the ✦
button and the answer appears; nothing is queued, nothing else in the workflow runs. The
node is meant to sit in a workflow permanently **bypassed or muted**, as a tool rather than
a step.

Typical uses: drafting or rewriting a generation prompt, translating one, describing a
reference image, or just asking a question with a picture attached.

Backends: an OpenAI-compatible `/v1/chat/completions` endpoint, Google's native Gemini
`generateContent`, or a local GGUF through `llama-cpp-python`.

## Install

```
cd ComfyUI/custom_nodes
git clone <this repository> ComfyUI-LLM-Widget
```

Restart ComfyUI. No Python dependencies are added.

For the GGUF format you additionally need `llama-cpp-python` in ComfyUI's environment, and
a `.gguf` under `models/text_encoders/` or `models/LLM/` — the same folders
ComfyUI-QwenVL-F and ComfyUI-MiniMaxH3-Easy scan, so a model installed for one of them is
found here too.

## The node

**LLM Widget** (category: `LLM Widget`)

| Slot | Type | Notes |
| --- | --- | --- |
| `image` (input) | `IMAGE` | optional |
| `video` (input) | `VIDEO` | optional |
| `text` (output) | `STRING` | optional — normally unused, see *Execution* |

Widgets:

- **toolbar** — ✦ run (becomes ■ stop while a request is in flight), elapsed time, ⧉ copy
  the answer, ⚙ settings. Below it, a chip per connected media shows what will actually be
  sent.
- **system_prompt** — free text, saved with the workflow. This is the only "template"
  there is.
- **question** — what you are asking.
- **answer** — where the result lands. Editable; saved with the workflow.
- **generate_on_execute** — off by default, see below.

### Connected media

The editor can only send media that already exists as a **file** in ComfyUI's
`input/`, `output/` or `temp/` folder. Wiring a `Load Image` or a video loader works;
wiring the output of a `VAE Decode` does not, because that image has no file until the
graph runs. Reroute nodes are followed through.

A chip under the toolbar tells you which it is — struck through and red means the media
will be skipped, and the run reports it as a warning rather than letting the model invent
a description.

Videos are sampled into 4 evenly spaced stills for the OpenAI and GGUF formats, since a
chat message has no video channel. Gemini receives the file whole.

### Execution

With `generate_on_execute` off (the default), running the graph just hands the stored
`answer` to the `text` output. This is what you want: the node has already done its work in
the editor.

Turn it on for a headless or API run. The question is then answered during execution, using
the actual `IMAGE`/`VIDEO` tensors (no file needed), and the result is pushed back into the
node's `answer` widget as well as onto the output.

## Settings

The ⚙ dialog is **installation-global**, not per-node — all LLM Widget nodes share one
backend. It is stored in `llm_widget.json` next to `nodes.py`, which holds a **plaintext
API key** and is gitignored.

| Setting | Applies to | Meaning |
| --- | --- | --- |
| API format | all | `OpenAI-compatible`, `Gemini`, or `GGUF` |
| API URL | openai / gemini | The path is completed for you: `http://host:1234` , `.../v1` and a full `/v1/chat/completions` all work |
| API key | openai / gemini | Sent as `Authorization: Bearer` / `x-goog-api-key` |
| Model | openai / gemini | For Gemini a bare id, `models/<id>` or a full model URL are all accepted |
| GGUF model | gguf | Any `.gguf` found under the scanned folders |
| Vision projector | gguf | `auto` picks the first `mmproj*.gguf` next to the model; `none` forces text-only |
| Context size | gguf | `n_ctx` |
| GPU layers | gguf | `n_gpu_layers`, `-1` offloads everything |
| Model thinking | all | `Off` (default) or `Keep` — see *Reasoning* |
| Temperature | all | |
| Max answer tokens | all | `max_tokens` for the local backends, the answer length cap |
| Unload after answering | gguf | Frees VRAM immediately instead of keeping the model resident |
| Send the connected image / video | all | Off means the question is asked as pure text |
| Describe each media in its own pass | gguf | See below |

### `Describe each media in its own pass` (GGUF)

Instead of putting the images and the whole question in one multimodal prompt, the model
first describes each connected media on its own with a short system prompt, then answers
the actual question from those descriptions with no images attached.

It is slower in total but each individual step is small, which matters because llama-cpp's
prompt-evaluation phase cannot be interrupted — a single large multimodal prompt is the
reason the editor can appear frozen for a while. It also gives cancellation a chance to
land between assets.

### Stopping

While a request is running the ✦ button becomes ■. Pressing it aborts the browser's fetch
*and* tells the server, which stops a local GGUF between tokens and releases the model. An
HTTP answer that arrives after a cancel is discarded. Model loading and llama-cpp's prompt
evaluation still cannot be interrupted.

### Reasoning

With **Model thinking = Off** (the default) the request carries every switch the backends
understand — `chat_template_kwargs.enable_thinking=false` for OpenAI-compatible servers and
llama-cpp, `thinkingConfig.thinkingBudget=0` for Gemini, `/no_think` for the Qwen family,
`force_reasoning=False` on the llama-cpp vision handler. An endpoint that rejects the
unknown field gets the request again without it, so a stricter API still answers.

Whatever a model emits anyway is cleaned up afterwards: everything up to and including the
**last** closing `</think>` is removed. The closing tag alone is enough on purpose — most
Qwen chat templates *pre-open* `<think>` in the assistant turn, so what comes back starts
with bare reasoning prose and the opening tag is never in the response. Harmony-style
`<|channel|>final<|message|>` markers are handled the same way.

Reasoning that carries **no marker at all** ("Here's a thinking process: 1. Analyze user
input…" as plain text) cannot be separated from the answer — nothing says where it stops.
If a model leaks like that despite the switches, use its non-thinking variant.

**Keep** sends none of the switches and strips nothing, for when you want to read the
reasoning.

A fenced answer is unwrapped only when the fence has no language or a prose-ish one
(` ``` `, ` ```text `, ` ```prompt `). A ` ```python ` block is left exactly as written, so
asking for code still gives you code.

## Development

- Python changes need a **full ComfyUI restart**.
- `web/*.js` changes need only a **browser hard refresh** (served via `WEB_DIRECTORY`).
- There is nothing to run without a ComfyUI installation; verification is manual on the
  canvas.

## License

MIT. Substantial parts of `nodes.py` and `web/llm_widget_ui.js` are adapted from
nkxx188/ComfyUI-MiniMaxH3-Easy, whose MIT license and copyright notice are kept in `LICENSE`.
