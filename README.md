# muse-glimmer

Serve [Muse Glimmer 30B](https://ai.meta.com/research/publications/muse-glimmer-30b-an-on-device-agentic-model/) locally. Development preview.

**Supported platforms:** macOS 26.2+ on Apple silicon. Requires Python 3.11+.

## Quickstart

```sh
pip install --pre muse-glimmer
muse-glimmer pull 1.0
muse-glimmer serve
```

Uses the 17G DFlash2 model with seven draft proposals. Serves at `http://127.0.0.1:8000`
with a live terminal HUD. **Ctrl+C** stops it.

<details>
<summary>Create a Python environment (Conda or uv)</summary>

With Conda:

```sh
conda create -yn glimmer python=3.12
conda activate glimmer
```

Or [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv venv --python 3.12 --seed .venv
source .venv/bin/activate
```

</details>

<details>
<summary>Install from source</summary>

Requires Git and Xcode with the Metal toolchain:

```sh
sudo xcodebuild -license accept
xcodebuild -downloadComponent MetalToolchain
git clone --recursive https://github.com/mergennachin/muse-glimmer.git
cd muse-glimmer
pip install -e .
```

</details>

## Python client

Save this as `foo.py`. Each `predict` call sends an independent prompt and
returns the assistant's final text as a string.

```python
from muse_glimmer import Glimmer

g = Glimmer(timings=True)
print(g.predict("Suggest a name for a coffee shop on the moon."))
```

Run the server and script together:

```sh
muse-glimmer serve foo.py
```

If a healthy Glimmer server is already listening on the configured host and port,
the script uses its loaded model and settings and leaves it running. Otherwise,
the launcher starts a server with your selected settings and stops it when the
script exits.
To try the example included in this checkout with your own prompt, run:

```sh
muse-glimmer serve examples/predict.py -- "Why does the moon have phases?"
```

With `timings=True`, each successful call prints the server's per-request TTFT,
prefill tok/s, and decode tok/s to stderr. TTFT
runs from the validated server handler to the first nonempty worker text. Prefill
measures newly processed prompt tokens, and decode measures generated tokens
using the worker's decode time. For programmatic access, `g.chat(messages)["timings"]`
contains the available `ttft_ms`, `prefill_tok_s`, and `decode_tok_s` values.
Older servers return token counts only. Restart with the updated server to
enable these measurements.

For a REPL, keep `muse-glimmer serve` running and open Python in a second terminal.
Set `system`, `temperature`, or `max_tokens` on a prediction. For a conversation,
pass the history to `chat`, which returns the full OpenAI-style completion
dictionary and accepts request options such as `max_tokens` and `tools`.

```python
messages = [{"role": "user", "content": "Suggest a name for a lunar coffee shop."}]
reply = g.chat(messages, temperature=0.7)
messages.append(reply["choices"][0]["message"])
messages.append({"role": "user", "content": "Give that shop a tagline."})
print(g.chat(messages)["choices"][0]["message"]["content"])
```

For a server on another port, use `Glimmer(base_url="http://127.0.0.1:8001/v1")`.
The default HTTP timeout is 300 seconds. Override it with `Glimmer(timeout=600)`.

## Coding agents

Install [Muse](https://dev.meta.ai/products/muse-code), [Pi](https://github.com/earendil-works/pi),
[Oh My Pi](https://omp.sh), [Hermes Agent](https://hermes-agent.nousresearch.com/),
or [OpenCode](https://opencode.ai), then launch from your project directory:

```sh
muse-glimmer serve muse
```

Replace `muse` with `pi`, `omp`, `hermes`, or `opencode` as needed.
The original `--launch muse` syntax also works.
No API key is needed. Exiting the agent stops the server.

<img src="img/muse-code-demo.png" alt="Muse coding agent building a Pomodoro timer with Muse Glimmer" width="600">

## Chat UI

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, set up and launch [Open WebUI](https://openwebui.com/):

```sh
uv tool install --python 3.12 open-webui==0.13.3
muse-glimmer serve --launch open-webui
```

Your browser opens at `http://127.0.0.1:8080`. Chats are saved locally. **Ctrl+C** stops both servers.

<img src="img/open-webui-demo.png" alt="Chatting with Muse Glimmer in Open WebUI" width="600">

## Smoke test

With the server running, open another terminal:

```sh
curl --fail-with-body http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "muse-glimmer-30B",
    "messages": [{"role": "user", "content": "Say hello."}],
    "max_tokens": 512,
    "temperature": 0.7
  }'
```

## Options

For model selection, downloads, and server options:

```sh
muse-glimmer pull --help
muse-glimmer serve --help
```

[default_config.toml](default_config.toml) documents the configuration settings.
