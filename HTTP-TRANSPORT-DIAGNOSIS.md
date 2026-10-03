# Blender MCP HTTP transport workaround

## Problem

On some Windows machines, MCP clients could not read the server's responses.
Curl, run with `--noproxy '*' --http1.1`, exited with code 56:

```
chunk hex-length char not a hex digit: 0x7b
```

The server itself was fine. The failure occurs **after Uvicorn writes the HTTP
response**: something on the machine replaces `Content-Length` with
`transfer-encoding: chunked` but does not chunk-encode the body. The client then
reads `{"` (`7b 22`) as a chunk size and fails. This is not invalid
initialization JSON.

## Evidence

1. A direct Python TCP socket received the same malformed response. Its body
   began with `7b 22` (`{"`), with no hexadecimal chunk length or terminal chunk.
2. A temporary instance of the same ASGI app was instrumented at both the ASGI
   send callback and Uvicorn's transport write. Both emitted a correct
   `content-length` header; the HTTP client instead received
   `transfer-encoding: chunked`. The JSON bytes were unchanged.
3. A bare TCP listener, with no MCP, Starlette, CORS, or Uvicorn, reproduced the
   rewrite when sending `Content-Length: 11` and `{"ok":true}`. Sending a
   properly chunked response through the same path preserved every byte.
   Different ports, header capitalization, request methods, and
   `Cache-Control: no-transform` did not resolve the length-framed case.
4. No environment, WinHTTP, or Windows Internet Settings proxy was configured.
   The likely cause is security software that hooks network traffic inside the
   process. This was **not conclusively attributed**: protection was not
   disabled to perform an A/B test.

Uvicorn's `http="auto"` selects `H11Protocol`, and a standalone h11 test
generated correct chunk sizes and the terminal zero chunk. The installed
sources for h11, Uvicorn, MCP, and Starlette matched their package RECORD
hashes, which rules out a modified library. No application code set
Transfer-Encoding.

## Change

`MCPHTTPFramingMiddleware` in [blender-mcp-server.py](blender-mcp-server.py)
removes Content-Length from responses on the MCP route. Uvicorn then generates
standards-compliant chunk framing itself, including the zero-length
`202 Accepted` notification response. JSON bodies, tool definitions, CORS,
stateless mode, and client URLs remain unchanged.

This is a narrow server-side compatibility fix for an external interception
bug; it does not repair or disable the interceptor. The application neither
sets Transfer-Encoding nor constructs chunks. This follows the
[ASGI HTTP specification](https://asgi.readthedocs.io/en/stable/specs/www.html#response-start-send-event),
which assigns transfer coding to the HTTP server when no Content-Length is
provided.

## Validation

- Curl initialize: HTTP 200, JSON decoded normally, exit code **0**.
- Actual wire body: `10e\r\n{"jsonrpc":...}\r\n0\r\n\r\n` (270 JSON bytes).
- Python standard-library `http.client`: initialize **200**,
  notifications/initialized **202** with an empty body, tools/list **200**;
  all three work on one persistent connection.
- Official MCP Python client: successful initialization and initialized
  notification with protocol `2025-11-25`. The explicit HTTP tests also
  validate protocol `2025-06-18`.
- All **51 tools** remain present. Names, descriptions, and schemas match the
  pre-change tool list exactly.
- Browser CORS preflight and cross-origin JSON response checks pass.

[test_http_transport.py](test_http_transport.py) covers these cases with real
network requests:

```powershell
python -m unittest -v test_http_transport
```
