# Prompt debug capture

> **Ticket:** [FRE-1614](https://linear.app/frenchforest/issue/FRE-1614) · **Code:** `src/personal_agent/telemetry/prompt_debug.py` (writer), `src/personal_agent/telemetry/prompt_debug_viewer.py` (viewer).

ES keeps only the size of a model call and a 100-character preview per message. The capture keeps the full text: every message, every tool definition, the request parameters and the reported usage, for every model call of every role (primary, planner, sub_agent, the extraction roles and all others).

## How to see a prompt

1. Turn the capture on. Add this line to the gateway `.env`, then restart the gateway:

   ```bash
   AGENT_PROMPT_DEBUG_CAPTURE_ENABLED=true
   ```

2. Send a turn.
3. Show the most recent trace:

   ```bash
   make prompt-show                              # same as ARGS="--last"
   make prompt-show ARGS="--trace <trace_id>"
   make prompt-show ARGS="--session <session_id>"
   make prompt-show ARGS="--trace <trace_id> --full"   # adds the complete tool JSON
   ```

   The target runs the viewer inside the gateway:

   ```bash
   docker exec cloud-sim-seshat-gateway /app/.venv/bin/python \
     -m personal_agent.telemetry.prompt_debug_viewer --last
   ```

4. Turn the capture off when you are done. Set the line to `false`, or delete it, then restart the gateway.

## What the viewer prints

For each call, in call order:

- A header: sequence number, role, model, placement (`cloud` or `local`), start time and duration.
- The usage that the provider reported (`prompt_tokens`, `completion_tokens`, cache tokens) and the request parameters.
- Each message, in full, with its role and an approximate token count.
- The tool block: the number of tools, the token count of the block, and one line per tool with its own count.
- A footer: the estimate (messages + tools) next to the reported `prompt_tokens`, with the difference in percent.

The counts use the cl100k encoding (`estimate_tokens`). They do not include the chat template's own tokens, so the estimate is usually a little under the reported `prompt_tokens`.

## Where the files are

`/app/telemetry/prompt_debug/<session_id>/<trace_id>/<seq>-<role>.json` in the gateway, on the `seshat_telemetry_cloud` volume. A system trace with no session goes under `no-session/`. One file holds one call.

- **What a file holds:** the arguments handed to `litellm.acompletion`. For a local call, the `extra_body` block shows as a nested object. The SDK puts its keys at the top level of the wire JSON.
- **Credentials:** only allow-listed parameters are written (`ALLOWED_PARAMS` in the writer). `api_key`, `api_base`, `client` and `extra_headers` are never written.
- **Privacy:** the files hold the owner's private content. They never go to ES. Files have mode `0600`, directories `0700`.
- **Retention:** files older than 7 days are deleted at gateway start and, while capture is on, at most once an hour during writes.
- **Failed calls:** a call that raises writes no file. The file count of a trace equals its count of `model_call_completed` events.
- **Off means off:** with the setting `false`, nothing is written. The request path does one setting read per call.
