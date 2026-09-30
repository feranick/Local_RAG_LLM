# Web search in Open WebUI — recommended setup

**Version 2026.9.30.1**

Lets a chat search the internet or read a URL you paste, alongside the document
collection. Settings below are the ones that worked on carbonio (breakerspace,
`qwen3.5:9b`, 16k context).

> **Privacy.** Documents stay local, but every web-search query — the text of your
> question — goes to the search engine. Keep web search a per-message choice.

## 1. Enable it

**Admin Panel → Settings → Web Search**

| Setting | Value | Why |
|---|---|---|
| Web Search | **on** | |
| Engine | **DuckDuckGo** | no API key; switch to SearXNG (self-hosted, most private) or Brave/Tavily (API key, most reliable) if it gets rate-limited |
| Search result count | **3** | each page takes context space; 16k fills fast |
| Bypass Embedding and Retrieval | **on** | pages go into the context whole, instead of the few chunks that best match the question — which are often menus and footers |
| Web Loader | **default** | Playwright needs a browser the Open WebUI image doesn't include; see Limits below |

Check that the container can reach the internet:

```bash
docker exec open-webui-breakerspace curl -sI https://duckduckgo.com | head -1   # 200 or 30x
```

## 2. Query generation

**Admin Panel → Settings → Interface**

- **Web search query generation: on.** A model turns the question into a search
  query; results are much better than searching the question verbatim.
- **Task Model: a small model without a thinking mode** (`llama3.2:3b`). With a
  thinking chat model as the task model, each hidden task call took ~1 minute.
- Retrieval query generation can stay **off** — that one is for the document
  collection, and questions search it fine as typed.

## 3. The preset

**Workspace → Models → preset → edit**

- **Capabilities → Web Search**: tick it, so the toggle is available with the preset.
- **System prompt**: add —

  > Your context may include web pages fetched for this question, in addition to
  > documents. Use them as sources. Never say you can't access websites; if a
  > page's content is missing or unusable, say that instead.

  Without it the model says "I cannot browse" even while quoting the page — a
  trained reflex, not a failed fetch.

## 4. Using it

- **Search:** in the message box, **+ → Web Search** for that message.
- **Read one page:** paste its URL into the message; it's fetched and added to the
  context.
- The answer's sources list documents and web pages together. Click a web source
  to see exactly what was fetched.

## Limits and troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "I cannot browse", but the URL is in the sources | trained disclaimer | the system prompt line above |
| Web source contains only menus/footer | page content loaded by JavaScript (large company sites) | use the vendor's PDF datasheet, or a page that serves plain HTML; or set up a Playwright service |
| No web sources at all | fetch failed | `docker logs --since 10m open-webui-breakerspace 2>&1 \| grep -iE 'loader\|playwright' \| tail` |
| Web content seems cut off | context overflow | fewer results; Admin → Documents → Top K ≈ 5; `journalctl -u ollama -f \| grep -i truncat` |
| Slow start before searching | task calls on a thinking model | small task model (section 2) |

**Playwright (optional).** Renders JavaScript pages, but only as a separate service
— the Open WebUI image has no browser, and selecting Playwright without one makes
every fetch fail (`Executable doesn't exist … chrome-headless-shell`). Its version
must match the library inside Open WebUI, re-checked after each update. Not needed
for most pages.
