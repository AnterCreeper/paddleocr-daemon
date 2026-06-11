# llama.cpp

This directory stores reference files for the host-side `llama-server`
deployment used by `paddleocr-daemon`.

Contents:

- `llama-server@.service`: systemd template for launching a `llama.cpp`
  OpenAI-compatible server instance
- `default`: example environment file for the PaddleOCR-VL-1.6 instance

Current assumptions:

- the service runs on the host, not inside the `paddleocr-daemon` container
- the model listens on `0.0.0.0:$SERVER_PORT`
- `paddleocr-daemon` reaches it through the Docker bridge gateway

Operational note:

- do not enable `--special` for the PaddleOCR-VL llama.cpp instance
- with `--special` enabled, the OpenAI-compatible response can leak the EOS
  token `</s>` into PaddleOCR markdown output
- the template defines role formatting and assistant turn boundaries, including
  the `eos_token` currently set to `</s>`

The example `default` file is configured for:

- `PaddleOCR-VL-1.6.gguf`
- `PaddleOCR-VL-1.6.mmproj.gguf`
- port `3456`
- `--chat-template-file chat_template.jinja`, which tells `llama-server` to use
  a Jinja chat template to render OpenAI-style `messages` into the model's
  actual prompt format

These files are documentation/reference artifacts for deployment and can be
copied into the real host-side `llama.cpp` installation as needed.
