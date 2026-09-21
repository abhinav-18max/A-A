# WebMCP on live sites — 2026-09-21

A&A 0.3.0, Playwright 1.63.0, Chromium 153.0.8010.12 (headless shell), macOS arm64. Sites
came from the [webmcp.com directory](https://webmcp.com/) (822 listed). Only read-only tasks
were run; no accounts, purchases or submitted forms. Re-run the survey with
`uv run python tools/webmcp_survey.py <url>...`.

## What each site exposed

| Site | Without the flag | With `webmcp=True` | Why |
|---|---|---|---|
| target.com | 1 tool (`search_products`) | same | Chrome origin trial. `filter_products` registers only on results pages |
| toban.app | 18 tools | same | Origin trial |
| playground.wordpress.net | 16 tools | same | Origin trial |
| developers.openai.com | none | 5 tools | Not in the trial; needs A&A's flag |
| computer.webmcp.com | none | 31 tools | Demo; needs the flag |
| render.com | none | none | Its script checks the removed `navigator.modelContext`; aliasing it (test only) registers 5 tools |
| coinranking.com | none | none | The directory lists 17 chart tools; none register on the home page |
| omio.com | HTTP 403 | HTTP 403 | Bot protection blocks headless Chromium |
| docket.io | none | none (API present) | No WebMCP; a site adapter was used instead |

With the flag on, third-party iframes (ads, analytics) also get the API but are blocked by
permissions policy. A&A reports them in `blocked_frames`; they are expected noise.

## Same task, two ways

Each path ran three times in a fresh session; timing starts after the page has loaded.

| Task | WebMCP path | UI path |
|---|---|---|
| Target: find coffee makers | `search_products` + wait + snapshot: **1.95 s median, 4 operations**. The tool returns only “Navigated to search results… use filter_products”; product names still come from a ~44 k-character page snapshot | fill search box + Enter + wait + snapshot: **1.97 s, 4 operations**, same products |
| OpenAI docs: pages about Realtime/WebRTC | `search_openai_docs`: **0.50 s median, 1 operation**, exact URLs, section hierarchy and snippets as 5 KB of JSON; the page does not change | open search dialog + wait + type + wait + 1 s for streamed results + snapshot: **2.43 s, 5 operations**, titles only from a 9 k-character snapshot |
| WordPress Playground: version and plugins | `playground_get_site_info` + `playground_list_files`: **0.08 s** → WordPress 7.1.1, PHP 8.3.33, `hello.php`, `index.php` | No equivalent in the UI for listing files |
| toban.app: who is on duty | `list_schedules` + `get_current_assignments`: **0.03 s**, IDs and names as JSON | Read the rendered roster |
| Docket: pricing page outline | No site tools. Site adapter `open_pricing` (click → wait → snapshot): **0.22 s**, landed on `/pricing`, five headings | Same steps, called directly |

Through the MCP server the same calls behaved identically (0.36 s on the OpenAI docs,
0.02 s on Target), with results marked untrusted and each invocation journaled.

## What WebMCP changes

1. **It depends on what the site chose to expose.** Tools come in two kinds. *Data tools*
   (OpenAI docs, toban, WordPress Playground) return structured results without touching
   the page. They were 5× faster, took one call instead of five, and returned facts the
   UI does not show. *UI-driving tools* (Target) run the site's own navigation and return a
   sentence; the agent still has to read the page, so speed and output are unchanged.
2. **A contract replaces selectors.** The Target UI path relied on the search box's
   accessible name (“What can we help you find…”); a copy change breaks it. The tool name
   and schema are published by the site. That is a robustness gain, not a speed gain.
3. **Tools are per page and change over time.** `filter_products` appears only after a
   search; coinranking's tools live on chart pages. render.com's tools broke when Chrome
   renamed `navigator.modelContext` to `document.modelContext` — being listed in a
   directory does not mean a site works in current Chrome.
4. **Flag versus origin trial matters for fidelity.** Origin-trial sites behave the same
   for A&A and for ordinary Chrome users. Sites that need the flag expose tools only to
   browsers that turn it on, so their tools are not what a normal visitor's browser sees.
5. **Safety metadata is sparse.** Only developers.openai.com declared `readOnly` and
   `untrustedContent` annotations. WordPress Playground offers `playground_delete_file` and
   `playground_execute_php`, toban offers `create_schedule` and `remove_member`, all
   without annotations. A harness cannot rely on annotations to find safe tools.
6. **Results are page-provided text.** Several sites return MCP-style
   `{"content":[{"type":"text","text":"{...json...}"}]}`, so the JSON must be parsed twice,
   and all output must be treated as untrusted data.
7. **Media is unaffected.** WebMCP exposes functions, not microphone, speaker, camera or
   display. A voice agent such as Docket's Aura still needs A&A's devices; WebMCP or an
   adapter can only replace the clicks around the conversation.
8. **Journaling is the one thing only WebMCP gives an observer.** DevTools reports every
   invocation, including ones made by the page's own code, so A&A's journal records what
   was called and what came back, not just which elements were clicked.

## A&A defects this run found (fixed)

- **Observation stream loss on bursts.** The worker treated gRPC backpressure (`write()`
  returning false, which object streams do after ~16 queued messages) as a slow consumer
  and closed the stream. toban.app registers 18 tools during startup, so every later event
  was silently dropped. Events now queue until `drain`; only a backlog of 1000 fails the
  session. Covered by `test_event_bursts_reach_the_journal` (fails without the fix).
- **Origin-trial sites were refused.** With the default `webmcp=False`, A&A returned
  `webmcp_disabled` although Target, toban and WordPress Playground expose tools in
  ordinary Chrome. The flag now only switches Chromium's feature on; listing, calling and
  journaling work whenever the page exposes `document.modelContext`.
- **Journal events lacked tool names.** DevTools reports only an invocation ID on
  responses; A&A now names the tool and its source on every `webmcp.*` event.
