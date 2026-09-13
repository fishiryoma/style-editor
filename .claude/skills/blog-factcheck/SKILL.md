---
name: blog-factcheck
description: >
  Runs the fact-check research step for tess's WordPress blog (the style-editor
  project). Use this skill whenever tess asks to fact-check, verify, or
  update a blog post from that project — for example "幫我查核文章 ID 6809",
  "查核這篇文章是不是過時了", "fact-check post 6809", "看一下這篇文章有沒有東西要更新",
  or any request that names a post_id together with checking/verifying/updating its
  content, prices, opening hours, business status, or links. Always use this skill for
  that task instead of improvising — the output must land in an exact JSON schema that
  the style-editor Flask app depends on (sessions/factcheck/{post_id}.json), and this
  skill encodes that schema plus the house rules for what to check and how to phrase
  findings. Trigger even if tess doesn't say "skill" or "factcheck" explicitly — any
  request to review one of her articles for outdated info, dead links, or "有沒有新資訊
  可以補充" against a factcheck_exports/{post_id}.json file should go through this
  procedure.
---

# Blog Fact-Check (事實查核)

## What this is

tess runs a personal Flask app ("style-editor") for editing her WordPress blog. It has
a 事實查核 (fact-check) tab: clicking "開始 / 重新查核" exports the article's content to
`factcheck_exports/{post_id}.json` and tells her to come ask Claude to check it. This
skill is that missing half — it reads the export, does the actual research, and writes
the result back in the exact format the app's review screen expects.

This is a **notes feature, not an editing feature**. Nothing gets written back to
WordPress and nothing gets auto-applied. The output is strikethrough-suggestions and
margin notes that tess reads and decides what to do with herself, by hand, in the WP
editor. Because of that, it's fine — good, even — to flag more rather than less. She
can ignore a note; she can't act on something you silently decided not to mention.

## Where things live

All paths below are relative to the style-editor project root (wherever you've
cloned/placed the `style-editor/` folder on disk).

- `factcheck_profile.md` — the house rules for what to check, what never to touch, how
  to write up findings, and source-citation standards. **Read this fresh every single
  run.** tess edits it over time as she refines what she wants, so don't rely on
  anything you remember from a previous run — always re-read.
- `factcheck_exports/{post_id}.json` — the input. Contains `post_id`, `title`, `slug`,
  `raw_original_content` (the raw WordPress HTML), `plain_text` (readable extraction),
  and `nodes` (a list of `{index, text, shortcode_map}` — every Japanese text node the
  app extracted from the HTML, in document order).
- `sessions/factcheck/{post_id}.json` — the output you write. This is read directly by
  the Flask app's review screen, so the schema has to match exactly (see below).

## Procedure

1. **Read `factcheck_profile.md` first, every time.** This defines the scope of what
   counts as checkable (prices, hours, business status, addresses, exchange rates,
   ranking/list claims, laws/regulations, event dates), what to never touch (personal
   voice, jokes, dialogue, style-guide "don't touch" items, the article's shortcodes),
   and exactly how findings should be written up. Everything below is a summary — the
   file itself is the source of truth and may have been edited since this was written.

2. **Read `factcheck_exports/{post_id}.json`.** Read the whole article via `plain_text`
   or `raw_original_content` to understand it as a piece of writing, not just a list of
   isolated facts — context matters for judging what's actually a claim worth checking.

3. **List everything worth checking**, per the profile's scope rules. Also actively
   look for new information tess would plausibly want to add to the article even where
   nothing is wrong — a nearby place that's opened since, a better/newer option in the
   same category, a detail readers would want that the article doesn't mention. This
   isn't limited to correcting mistakes.

4. **Check every `<a href="...">` link** in `raw_original_content` by fetching it.
   Three outcomes, and it matters which one you report:
   - Opens fine → say nothing, move on.
   - Confirmed broken (404, dead domain, page taken down) → node-anchored note saying
     which URL and that it looks dead. Never touch the `href` itself — that's tess's
     call, not something to silently rewrite.
   - Can't tell either way (Facebook, Instagram, and similar platforms block automated
     fetches) → note that the platform blocks automated checks and she should click it
     herself. Do **not** report this as "broken" — that's a false claim, just because a
     scraper got blocked doesn't mean the link is dead.

5. **Research each item with web search**, prioritizing official sites, government
   pages, or mainstream news over blogs/forums. If only a non-official source turns up,
   still use it — but say so plainly in the note's `sources[].note` field ("非官方來源")
   rather than presenting it as authoritative.

6. **Write up findings as node-anchored entries** — every note or diff attaches to a
   specific `nodes[].index` from the export, placed next to the paragraph it's about.
   Never produce a disconnected top-level list; tess reads inline, in context. Two
   shapes, both described in the schema below:
   - **Diff-style**: original wording struck through, replacement suggested inline —
     for cases where you're confident about the new value. Keep the diff scoped to the
     factual span only; don't touch surrounding phrasing or tone.
   - **Pure-note style**: no diff, just a note — for uncertain/contradictory findings,
     wholesale-outdated situations (permanently closed shop, etc. — flag, don't
     rewrite the paragraph), link issues, or "worth adding but too big to inline as a
     diff" suggestions.
   Any replacement text you do write must sound like tess, not like you — keep her
   phrasing register, sentence length, and tone. You're fact-checking, not copyediting;
   leave grammar/wording alone even mid-sentence next to a fact you are changing.

7. **Write `sessions/factcheck/{post_id}.json` once, at the end**, after all research
   is done — not incrementally while you're still checking things. See schema below.

8. **Report back in chat** — not a full file dump. A short summary: how many
   diff-suggestions vs. plain notes, one line per finding, and a reminder to open the
   style-editor app's 事實查核 tab to look at it visually (there's no apply button for
   this feature; it's read-only until she edits WordPress herself).

## Output schema

`sessions/factcheck/{post_id}.json` must have exactly this shape:

```json
{
  "post_id": 6809,
  "title": "... (copied verbatim from the export)",
  "slug": "... (copied verbatim from the export)",
  "raw_original_content": "... (copied verbatim from the export, unmodified)",
  "nodes": [
    {
      "protected_original": "...",
      "protected_corrected": "...",
      "shortcode_map": {},
      "statuses": []
    }
  ],
  "prepared_at": "2026-07-11T17:48:21",
  "applied": false,
  "kind": "factcheck"
}
```

`nodes` must have exactly one entry per node in the export, **in the same order**, with
`shortcode_map` copied from the matching export node. For a node with nothing to say:
`protected_original` and `protected_corrected` are identical, `statuses` is `[]`, and
there's no `note` or `sources` key at all (omit them, don't set them to `null`/`""`).

For a **diff-style** node: `protected_corrected` differs from `protected_original`,
`statuses` has one `"accepted"` string per changed span (almost always just `["accepted"]`
for a single change), and `sources` lists where the new value came from:

```json
{
  "protected_original": "営業時間：11:00-14:00 / 17:00-20:00、定休日：日曜日。",
  "protected_corrected": "営業時間：11:00-14:00 / 17:00-20:00、定休日なし（2026年7月確認）。",
  "shortcode_map": {},
  "statuses": ["accepted"],
  "sources": [
    {"note": "官方網站 店鋪資訊（現況：週一至週日營業）", "url": "https://example.com/..."}
  ]
}
```

For a **pure-note** node: `protected_original` equals `protected_corrected`, `statuses`
is `[]`, and `note` carries the finding (with `sources` if you have any):

```json
{
  "protected_original": "「雙月」はもう6年間ビブグルマンに掲載されていて",
  "protected_corrected": "「雙月」はもう6年間ビブグルマンに掲載されていて",
  "shortcode_map": {},
  "statuses": [],
  "note": "「6年間」は執筆時点ではおおよそ正しかったですが、その後の報道では「7年」「8年」と情報が一致しません。最新の連続年数はご自身でご確認のうえ書き換えてください。",
  "sources": [
    {"note": "2024年10月の報道（「連続7年」）", "url": "https://example.com/..."},
    {"note": "別の情報源（「連続8年」）", "url": "https://example.com/..."}
  ]
}
```

`prepared_at` is an ISO timestamp (`datetime.now().isoformat(timespec="seconds")`
style — no timezone suffix) for when you wrote the file. `applied` is always `false`
(only the app sets this, when — for the grammar kind, not this one — content actually
gets written to WordPress; factcheck never does that). `kind` is always the literal
string `"factcheck"`.

## Things that will break the app or defeat the point if you get them wrong

- Don't touch WordPress or the database — this skill only ever reads and writes local
  JSON files.
- Don't rewrite a `href` — even a confirmed-dead one. Note it; don't fix it.
- Don't touch subjective/personal-voice text — only factual, verifiable claims are in
  scope. If a sentence mixes a fact with a personal reaction, touch only the factual
  part.
- Don't guess at a value you're not confident in and silently swap it in as a diff —
  use a pure-note instead so tess sees your uncertainty and the conflicting sources,
  rather than a confident-looking strikethrough for something you're not sure about.
- Don't write the session file more than once per run, and don't write it partway
  through — finish all the research first, then write once.
- Don't drop a node from the `nodes` array or change its order/count relative to the
  export — the app matches nodes to the original HTML by position, so a mismatch will
  misplace every note after it.
