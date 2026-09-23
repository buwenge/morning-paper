# morning-paper

A fully autonomous "AI morning briefing" pipeline: scheduled collection →
a headless LLM that researches the web on its own and writes the issue →
a headless LLM that transcribes every linked article into a clean local
archive → delivery into a chat companion, with reader feedback (ratings,
follow-up requests, taste lists) fed back into the next day's writing.

This is a sanitized extract from a larger personal companion-bot project
("小予晨报" — Xiaoyu's Morning Paper). The companion's own voice/persona is
left intact in the copy (ratings intro text, delivery framing) since that is
part of what makes the design interesting to read; only real infrastructure
— paths, tokens, internal doc references — has been scrubbed. See
[Sanitization](#sanitization) below.

## Pipeline

```
1. Collect        morning_scout_sources.py   deterministic, no LLM
   (HN Algolia / lobste.rs / a Douban group / X syndication for single tweets)
        │
        ▼
2. Write           morning_scout.sh (stage 2)   headless `claude -p --model sonnet`
   (reads material + prompts/morning_scout.md, free WebSearch/WebFetch, writes
    a scout-<date>.json draft — this is the only LLM step with editorial judgment)
        │
        ▼
2.5 Draft check     morning_scout_repair.py      deterministic, no LLM
   (the writer occasionally leaves unescaped `"` inside a digest and the file
    isn't valid JSON; this step re-escapes stray quotes by JSON grammar, keeps
    the original as `*.bak-badjson-<date>`, and fails loudly if it can't fix it)
        │
        ▼
3. Archive          morning_archive.py           headless `claude -p --model haiku`, per item
   (each linked article gets its own isolated transcription session; X tweets
    are assembled deterministically from the syndication text already collected
    in stage 1, or fetched from the syndication endpoint on the spot if stage 1
    missed them — no LLM call needed for those. If WebFetch is blocked by the
    site, the page is downloaded directly, stripped to plain text with the
    standard library, and haiku transcribes the local file instead)
        │
        ▼
4. Deliver + feedback   morning_paper.py + morning_feedback.py   inside the daemon
   (schema-validate, dedupe against history, trim to a section-priority budget,
    format into the wakeup message; reader ratings/follows/taste-list feed back
    into stage 2's next run via the `feedback` block in the material file)
```

Readers can rate each item 0–3, ask to follow up on one, and curate a
prefer/avoid taste list in plain language (`home 晨报 打分 2:3追 5:0`, or
"第二条 3" — both Arabic and Chinese numerals are accepted). None of this
needs a database: everything lives in a couple of JSON files with atomic
writes and file locks.

## What's included

- **Core modules** (root of this repo, no changes needed to reuse):
  `morning_paper.py`, `morning_feedback.py`, `morning_archive.py`,
  `morning_scout_sources.py`, `session_intro.py`, `cn_numerals.py`,
  `log_store.py` (the tiny JSONL activity log the cron stages write
  "briefing run started / draft landed / stage failed" entries to, so the
  chat UI's activity panel shows them without reading the cron log)
- **Cron shell wrapper**: `morning_scout.sh` (stages 1–3) and the stage-2.5
  draft check `morning_scout_repair.py`
- **The stage-2 writer's task brief**: `prompts/morning_scout.md`
- **Frontend reading page** (Next.js/React, TypeScript): `frontend/`
- **Chatbot integration excerpts** (see caveat below): `integration/`
- **Tests**: `tests/` (five suites test the core modules directly and run
  as-is; two production test files that exercise the full chat-command
  dispatcher and WebSocket router aren't included as runnable files — see
  `tests/README_excerpted_tests.md` for what they cover)

`integration/*.py` are **excerpts from a larger production `daemon.py` /
`home.py` / `ws_handlers.py`**, not complete importable files — they show
where and how the four core modules get wired into a scheduler, a chat
command dispatcher, and a WebSocket handler table. Adapt them to your own
bot's architecture rather than importing them directly.

## Security boundary (the part worth reading even if you don't reuse the code)

Both LLM steps (stage 2's writer, stage 3's per-article transcriber) run
against **untrusted external content** — scraped web pages, PDFs, social
posts — so they're deliberately sandboxed:

- Working directory is a directory kept **permanently empty** (no
  `CLAUDE.md`, no `.claude/`) — the writer/transcriber inherits none of the
  companion's own prompt or permissions.
- Launched with `env -i`, which clears all inherited environment variables;
  credentials are passed back in explicitly and minimally (one OAuth token,
  nothing else).
- `--tools`/`--allowedTools` scope file access down to **the single file
  each session is allowed to touch** (not a whole directory) using path
  globs, verified empirically that a glob like `Edit(//path/scout-*.json)`
  really does reject writes to any other filename in that directory.
- `Bash` is physically excluded from `--tools` for every one of these
  sessions — it is never even offered as an option, not just denied by
  policy.
- Every prompt sent to these sessions states explicitly that the fetched
  content is untrusted data to transcribe, not instructions to follow; the
  core modules additionally hard-filter obvious prompt-injection phrases
  ("ignore previous instructions", "system prompt", etc.) out of anything
  that reaches the final issue.
- A single article's fetch/transcription failing or timing out only drops
  that one article's archive (it falls back to a plain link); it never
  fails the day's issue or the rest of the archive batch. The per-item reason
  (including haiku's own "couldn't fetch this" note) goes into the activity
  log line. No new direct-download fallback is started in the last few
  minutes before the daemon reads the manifest, so a slow night can't delay it.

## Deploying this yourself

1. Set `MORNING_PAPER_ENABLED=true` in your `.env` (see `.env.example`) —
   every entry point (`is_enabled()`) checks this and no-ops otherwise.
2. Put a Claude Code OAuth token somewhere `morning_scout.sh` and
   `morning_archive.py` can read it (`TOKEN_FILE`, currently pointed at
   `/etc/xiaoyubot/claude-oauth-token.env` — change this constant to wherever
   you actually keep it). Both files fall back to whatever ambient
   credentials the `claude` CLI would otherwise use if the file is absent.
3. Create the sandbox working directory referenced by `SCOUT_WORKDIR` in
   both `morning_scout.sh` and `morning_archive.py` (currently
   `/opt/xiaoyubot-scout`) — **it must stay empty**, no `CLAUDE.md`, no
   `.claude/`.
4. Point `morning_scout.sh`'s hardcoded absolute paths (material/draft/prompt
   file locations, the `--allowedTools` globs) at wherever you actually
   deploy the core modules — they assume they live next to `.morning_paper/`
   and `prompts/`.
5. Add a cron entry that runs `morning_scout.sh` once daily before your
   delivery window (production runs it at UTC 21:10 = 05:10 Beijing time;
   all business-date math in these modules is pinned to UTC+8 — see the
   `BIZ_TZ` docstring in `morning_scout_sources.py` if you're in a different
   timezone and need to change that constant).
6. Wire the four core-module entry points into your own bot using
   `integration/` as a reference: a scheduled "prepare" job
   (`morning_paper.prepare_issue`, cron'd for `PREPARE_HOUR`/`PREPARE_MINUTE`),
   a wakeup-time injection (`morning_paper.issue_for_wakeup` +
   `format_injection`, then `mark_delivered` once your bot's reply actually
   went out), a chat command handler for ratings/follows/taste list
   (`morning_feedback.*`), and a read-only WebSocket query for the frontend
   page (`morning_paper.load_issue_for_date`/`list_scout_dates` +
   `morning_feedback.frontend_summary`).
7. Build `frontend/` into your own Next.js app (it expects the same
   `get_morning_paper` → `morning_paper` WebSocket message pair documented
   in `integration/frontend_integration_notes.md`, and the TypeScript types
   listed there).

## Dependencies

The core modules are **standard-library-only by design** — `cn_numerals.py`
and `morning_feedback.py` in particular are explicitly written to be
importable from two different Python interpreters (a venv running the chat
bot, and the system `/usr/bin/python3` running the cron collector), so
neither pulls in any third-party package. `morning_scout_sources.py` and
`morning_archive.py` shell out to the `claude` CLI as a subprocess rather
than calling an SDK.

## Local test run

```
pip install pytest   # the only third-party dependency, for the test suite itself
python3 -m pytest tests/
```

All four included suites run against real fixture data captured from a
production run (`tests/fixtures/`, two small JSON files of ordinary public
tech-news content — nothing private in them) rather than hand-built
approximations of the schema.

## Sanitization

What's been changed from the original production code:
- Real deployment paths (`/root/xiaoyu` → `/opt/xiaoyubot`, the writer
  sandbox directory, the OAuth token file path) are replaced with
  placeholders — update the constants for your own deployment.
- Internal-only references (a specific internal design-doc path, an
  unrelated internal service name mentioned only as a "same credential
  source as ___" aside) are removed or genericized.
- One unrelated feature that shares a code path inside the production
  `do_wakeup()` function (a private companion easter egg, out of scope for
  this repo) is entirely excluded from `integration/daemon_integration.py`,
  not merely trimmed.

What's deliberately **not** changed: the companion's persona and voice in
user-facing strings (the ratings intro, delivery framing, error messages)
are left as-is rather than genericized, since they're an example of a
specific design choice, not a secret.

## License

MIT. See `LICENSE`.
