# muse-glimmer

Serve [Muse Glimmer 30B](https://ai.meta.com/research/publications/muse-glimmer-30b-an-on-device-agentic-model/) locally. Development preview.

## Supported platforms

- macOS 14+ on Apple silicon

## Install

Use an existing Python 3.11+ environment, or create one with **Conda**:

```sh
conda create -yn glimmer python=3.12
conda activate glimmer
```

Or with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv venv --python 3.12 --seed .venv
source .venv/bin/activate
```

### From PyPI

```sh
pip install --pre muse-glimmer
```

### From source

Requires Git and Xcode. After installing or updating Xcode, set up the Metal toolchain:

```sh
sudo xcodebuild -license accept
xcodebuild -downloadComponent MetalToolchain
```

```sh
git clone --recursive https://github.com/mergennachin/muse-glimmer.git
cd muse-glimmer
pip install -e .
```

## Serve

```sh
muse-glimmer pull 1.0
muse-glimmer serve
```

Use `pull 1.0 --variant <folder-name>` to choose a specific folder,
or `serve --model <path>` for a local model.
Use `pull 1.0 --force` to re-download a cached model.
Use `pull --list` to see model versions, variants, and the selected artifact's location.
Use `pull --remove` to delete the default downloaded model; other variants and shared data
they still need are kept.

The server runs at `http://127.0.0.1:8000` with a live terminal HUD. **Ctrl+C** stops it.

## Run Muse (coding agent) with the local model

With [Muse](https://dev.meta.ai/products/muse-code) installed, run this from your project directory:

```sh
muse-glimmer serve --launch muse
```

No API key is needed. Exiting Muse stops the server.

<img src="img/muse-code-demo.png" alt="Muse coding agent building a Pomodoro timer with Muse Glimmer" width="600">

## Run Pi with the local model

Install [Pi](https://github.com/earendil-works/pi) once, then launch from your project directory:

```sh
npm install -g @earendil-works/pi-coding-agent
muse-glimmer serve --launch pi
```

No API key is needed. Exiting Pi stops the server.

## Run Oh My Pi with the local model

With [Oh My Pi](https://omp.sh) installed, run this from your project directory:

```sh
muse-glimmer serve --launch omp
```

No API key is needed. Exiting Oh My Pi stops the server.

## Run Hermes Agent with the local model

With [Hermes Agent](https://hermes-agent.nousresearch.com/) installed, run this from your project directory:

```sh
muse-glimmer serve --launch hermes
```

No API key is needed. Exiting Hermes stops the server.

## Run OpenCode with the local model

With [OpenCode 2](https://opencode.ai) installed, run this from your project directory:

```sh
muse-glimmer serve --launch opencode
```

No API key is needed. Exiting OpenCode stops the server.

## Chat with Open WebUI

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Open WebUI](https://openwebui.com/) once:

```sh
uv tool install --python 3.12 open-webui==0.13.3
muse-glimmer serve --launch open-webui
```

Your browser opens at `http://127.0.0.1:8080`. No login is needed; chats are saved locally.
**Ctrl+C** stops both servers.

<img src="img/open-webui-demo.png" alt="Chatting with Muse Glimmer in Open WebUI" width="600">

## Smoke test

With the server running, open another terminal:

```sh
curl --fail-with-body http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "muse-glimmer-30B",
    "session_id": "demo",
    "messages": [
      {"role": "system", "content": "Reasoning strength: low"},
      {"role": "user", "content": "Say hello."}
    ],
    "max_tokens": 512,
    "temperature": 0.7
  }'
```

A successful response contains an assistant message in `choices[0].message.content`.
Requests appear under `demo` in the HUD; change `session_id` for another session.

From a source checkout, test concurrent traffic, tools, parallel child agents, and
prefix caching against a dedicated server started with `muse-glimmer serve`:

```sh
python scripts/smoke_test.py
```

Add `--requests 24` for sustained traffic, `--base-url` for another server,
or `--output smoke.json` to save results.

## Configuration

[default_config.toml](default_config.toml) documents all settings. Load overrides with:

```sh
muse-glimmer serve --config ./config.toml
```

Run `muse-glimmer serve --help` for command-line options.
