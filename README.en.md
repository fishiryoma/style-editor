# Japanese Proofreading Tool (style-editor)

[繁體中文](README.md) | **English**

A full-stack local tool that reads directly from the **Local WP (local WordPress development environment) MySQL database**. It uses the **Gemini API** for paragraph-by-paragraph Japanese proofreading and a **Claude Code skill** for fact-checking, and tracks review progress. Japanese proofreading sends the post to the AI one **H2 section** at a time (in either "light edit" or "rewrite" mode); fact-checking works on HTML text nodes. Nothing is written back to the database until you've reviewed the suggestions. The original is backed up automatically before it's overwritten, so every change stays under your control and can be rolled back.

> **Why I built this**: I'm a non-native speaker learning Japanese, and I still write my blog about daily life and travel in Japanese. Most AI proofreading tools "correct" text into textbook-standard Japanese, erasing the nuance, casual tone, and kaomoji that come naturally when a Taiwanese writer writes in Japanese. So I built this tool to have the AI flag only genuine grammar and word-choice errors while keeping my own writing style intact.

## Three Modes

The web page has three tabs on the left. Each one works independently:

![style-editor screenshot](./images/preview.png)

### 📝 Japanese Proofreading

Uses Gemini to proofread Japanese according to your writing style and to check/suggest titles. The post is processed one **H2 section** at a time, and you accept or keep the original for each whole section. The original post in the WordPress database is only overwritten after you've reviewed everything.

There are two modes. Pick the default mode for the whole post before starting; during review, any section can be re-run in the other mode:

| Mode | Best for | What the AI does | How it's shown |
| ---- | -------- | ---------------- | -------------- |
| **Light edit** (微調) | Topics you know well; small fixes only | Fixes typos, conjugation, particles, kanji/kana usage; paragraph count and order stay the same | Strikethrough / new text inline |
| **Rewrite** (段落重寫) | Unfamiliar topics where sentences need reordering | Reorders, merges, and splits sentences and paragraphs within a paragraph group | Original and rewrite side by side, plus a one-line explanation |

When rewriting, images, embeds, Cocoon boxes, H3 subheadings, and styled paragraphs **stay in place** (their text only gets a light edit). The AI can only reorder text between two fixed elements, so it never moves one image's description under another image. Bold, links, and colors move together with their sentences.

- Only fixes "genuine errors": typos, conjugation errors (e.g. 「楽しいでした」→「楽しかったです」), particle problems that make the meaning unclear, and so on.
- It does **not** rewrite your Japanese into textbook-standard Japanese. The nuance, casual tone, kaomoji, etc. that are typical of Taiwanese writers are preserved.
  This judgment logic lives in [`style_profile.md`](./style_profile.md), which was derived by analyzing your existing posts. **You can edit this file directly** to adjust how the AI judges your writing (including title style).
- After reading the whole post, it checks the title for grammar issues. If the title is fine and it can't think of anything better, it won't force 3 suggestions.
  Only when there's a grammar problem, or a phrasing that better fits your style, will it give you exactly 3 title suggestions to choose from (or you can keep the original).
- Before overwriting the original, it always backs up the title and content to `style-editor/backups/`, so you can recover the original at any time.
- Before overwriting, it checks whether the post's content or title has been changed in the WordPress admin. If so, it asks you to proofread again instead of overwriting your manual edits.

See "Japanese Proofreading: Step by Step" below for detailed instructions.

### 🔍 Fact-Checking

This tab **does not call any AI**. It simply exports the post's content to a file (`style-editor/factcheck_exports/{post ID}.json`).

- After you click "Start Fact-Check", the page just tells you where the file was exported. Next, **go to a Cowork chat yourself and ask Claude to fact-check the post** (e.g. "幫我查核文章 ID 6809", i.e. "fact-check post ID 6809").
- The actual fact-checking rules and output format are defined in [`.claude/skills/blog-factcheck/SKILL.md`](./.claude/skills/blog-factcheck/SKILL.md). This is a **project-level skill**: it's detected automatically whenever you open Claude Code in the `style-editor` project folder.
- When it's done, Claude writes the results directly to `style-editor/sessions/factcheck/{post ID}.json`. Refresh this web page and you'll see the fact-check notes next to the corresponding paragraphs.
- **Why not call an API directly like the proofreading tab does, instead of handing it to Claude manually?** Fact-checking relies much more on "searching the web + judging how trustworthy information is" than grammar proofreading does, and Claude is clearly stronger than Gemini at this. But calling the Claude API directly from the code costs extra, so as a compromise I reuse the Claude I'm already using (packaged as a skill to keep the fact-checking process consistent), rather than integrating a paid API into this tool.
- This is a "notes feature": there are no accept/keep-original buttons, and results are **never applied back to WordPress automatically**. The fact-check results are just for you to read; if you decide something needs changing, edit the post manually.

### 🗂️ Draft Management

Lists the progress of all drafts created by proofreading and fact-checking (of either kind). Each row has a running number so you can see the total at a glance. You can see each draft's status and timestamp, delete a single draft, or tick several (the header checkbox selects all) and click "Delete selected" to remove them in one go. Deleting here only removes the local review progress files; it never touches the posts in the WordPress database.

## Installation

Prerequisite: Local WP must be running (so that MySQL is started).

```powershell
cd "<your-blog-project-folder>\style-editor"
pip install -r requirements.txt --break-system-packages
```

If your environment doesn't need `--break-system-packages`, just remove it.

## Setting Up the API Key (Gemini, free)

Go to https://aistudio.google.com/apikey, sign in with your Google account, and create a free Gemini API key.

Copy [`.env.example`](./.env.example) to the **parent folder** (your blog project root) and rename it to `.env`:

```powershell
cp .env.example ../.env
```

Open `<your-blog-project-folder>\.env` and set `GEMINI_API_KEY` to your key:

```
GEMINI_API_KEY=AIzaSy........
```

**Current model**: `gemini-3.5-flash-lite` (switched in September 2026; see the background in "Sections, Nodes, and Rate Limits" below — its free quota is considerably larger than `gemini-3.5-flash`'s).
If you later hit errors because the free quota is used up, you can change `GEMINI_MODEL` in `.env` to another model (e.g. the older `gemini-3.1-flash-lite`).
Free-tier quotas may change over time; check your account's current quota at https://aistudio.google.com/rate-limit.

The DB connection settings are read from the same `.env` (shared with `sync-analytics.py`), so there's nothing else to configure.

## Running

```powershell
cd "<your-blog-project-folder>\style-editor"
python app.py
```

Open http://localhost:5055 in your browser.

## Tests

```powershell
cd "<your blog project folder>\style-editor"
python -m unittest discover -s tests
```

Gemini and the database are faked in the tests, so no API calls are made and no real posts or drafts are touched.

## Japanese Proofreading: Step by Step

1. Select a post from the list on the left.
2. In the top right, pick a mode: **微調 (light edit)** for topics you know, or **段落重寫 (rewrite)** for unfamiliar topics that need sentence reordering. Your last choice is remembered.
3. Click "Start / Re-run AI Proofreading". The AI does two things at once (one API call per section, so longer posts take longer):
    - Proofreads the body section by section
    - Reads the whole post, then checks the title and suggests alternatives if appropriate
4. **Title section**: The title grammar check result appears below the title. If there are suggestions, you'll see "Keep original title" plus up to 3 new titles; pick the one you want with the radio buttons (defaults to keeping the original).
5. **Body section**: Each H2 section is a card. Its header shows the section number, mode, change status, and buttons:
    - ✓ Accept: accept the AI's changes for the whole section (default)
    - ✕ Keep original: reject the whole section
    - ✎ Redo: enter feedback (e.g. "this sounds too stiff", "put the conclusion first") and the AI redoes this section, using its previous result as a reference
    - Re-run as rewrite / light edit: redo this section from the original in the other mode
    - Light-edit cards: green underlines are suggested edits, red strikethrough is the original (only text is shown for readability; bold and other formatting are hidden here but kept when applying).
    - Rewrite cards: original on the left, rewrite on the right, with the AI's explanation in the purple box above. Rewrites change more, so compare each section to make sure the meaning hasn't changed.
    - An orange warning means part of the AI's output failed the format check (e.g. a bold marker went missing). Those parts automatically keep the original text; click "Redo" to try again.
6. Once everything looks good, click "Apply and Overwrite Original" in the top right. The post content (and your chosen title, if it isn't the original) is overwritten, and the original version is automatically backed up to `style-editor/backups/`. Sections with no changes, or kept as original, are preserved character for character.
7. You can close the browser at any time. When you reopen the page and select the same post, your progress is still there (stored in `style-editor/sessions/`; nothing affects WordPress until you apply).

> Proofreading drafts created before November 2026 use the old per-edit review format, which the new version can't read; the page will ask you to re-run proofreading. Old drafts can be deleted in Draft Management.

## Restoring a Backup

`style-editor/backups/{post ID}-{slug}-{timestamp}.json` contains the original `{title, content}` from before the overwrite.
To restore, open phpMyAdmin / Adminer in Local WP and paste the backup's contents back into the post's `wp_posts.post_title` / `post_content` columns.

## Known Limitations

- Accept / keep original works on a whole H2 section; you can't accept just one of the edits inside a section. For parts you disagree with, tell the AI with "✎ Redo".
- In rewrite mode the AI can only reorder text between two fixed elements (images, H3s, boxes, etc.). To move text across an image, edit it yourself in WordPress.
- When a rewrite merges or splits paragraphs, the new paragraphs are always plain, unstyled paragraphs (styled paragraphs never take part in rewrites, so no styling is lost).
- Only the body and the title are processed. SEO meta, category names, and other fields are not proofread.
- There's no user login. It's meant only for your own local use — don't expose this web page publicly (e.g. don't publish it with tools like ngrok).
- Gemini's free quota is limited (varies by model, with caps on requests per minute and requests per day). Long posts, or proofreading several posts in quick succession, may hit the limit; if you see an error message, wait a moment and try again. See "Sections, Nodes, and Rate Limits" below for details.

## Sections, Nodes, and Rate Limits (Technical Notes)

### Background: Why These Measures Exist

In September 2026, the model in use was `gemini-3.5-flash`, with a free quota of **RPM 5, TPM 250K, RPD 20** (live numbers from
https://aistudio.google.com/rate-limit). RPD (requests per day) was only 20 and shared across the entire
API project, so it was hit quickly in practice. The batching, per-minute throttling, 429 retries, and output limits listed
under "Current Measures" below were all designed around that quota.

After switching to `gemini-3.5-flash-lite` on September 12, 2026, the same account's free quota became **RPM 15, TPM 250K,
RPD 500** — RPD went from 20 to 500 (25×) and RPM from 5 to 15 (3×). The RPD bottleneck that used to be hit first
should no longer come up in normal use. The default for `GEMINI_RPM_LIMIT` has been updated to 15 accordingly; batching and 429 retries
were **kept** as safeguards (the quota is still finite, just more generous).

### Japanese Proofreading: One H2 Section at a Time (`section_diff.py`)

Since November 2026, Japanese proofreading no longer uses text nodes. It works on **H2 sections** instead: section 0 is the intro before the first H2, and each H2 plus the content under it is one section. Text nodes were too fragmented (one sentence split into several pieces by bold text and links), so the AI only saw half sentences and could only fix wording, never reorder sentences.

How it works:

1. **Splitting**: Posts are made of Gutenberg blocks (wrapped in comments like `<!-- wp:paragraph -->`). The top-level blocks are split out first, then grouped into sections at each top-level H2 heading block.
2. **Classifying**: Plain paragraphs (`<p>` with no styling) can be rewritten, and adjacent plain paragraphs form a "group". Images, H3s, styled paragraphs, Cocoon boxes, lists, etc. are fixed elements: they don't move, and their text only gets a light edit.
3. **Markers**: Inline tags such as `<strong>`, `<a>`, and `<span>` become paired `⟦T1⟧…⟦/T1⟧` markers, `<br>` becomes `⟦T2/⟧`, and shortcodes become `⟦S3⟧`. When the AI reorders sentences, the markers move with them.
4. **Validation**: After the AI responds, the tool checks that every marker appears exactly once, stays paired within the same paragraph, and that no new HTML tags appeared; in light-edit mode it also checks that the paragraph count is unchanged. Failing parts are retried once automatically; if they still fail, they keep the original text and the page shows a warning. The AI tends to swap "～" for the look-alike "〜" and "..." for "…"; the tool automatically restores the original form.
5. **Rebuilding**: Unchanged parts use the original string (not a single character changes); only changed parts are replaced. Rewritten groups are re-wrapped as `<!-- wp:paragraph --><p>…</p><!-- /wp:paragraph -->`.

A post has roughly 5–8 H2 sections (the current 201 posts average about 6.4; the longest section is about 3,200 characters). With one API call per section plus 1 for title suggestions, that's about **6–9 requests** in total, below `GEMINI_RPM_LIMIT` (default 15), so normally one click of "Start / Re-run" processes the whole post. Posts with more than 15 sections enter the resume flow ("X/Y sections done, please wait and click again").

Rewriting needs stronger restructuring ability. If `gemini-3.5-flash-lite` doesn't rewrite well enough, set `GEMINI_REWRITE_MODEL` in `.env` to use a stronger model for rewrite mode only (light edits and title suggestions still use `GEMINI_MODEL`).

### Fact-Checking: One "Node" at a Time (`html_diff.py`)

Fact-checking still uses `extract_nodes` in `html_diff.py` to split the post's HTML into individual "nodes". **A node is a single run of continuous text (an HTML text node), not an entire paragraph**: if a `<p>` paragraph contains inline tags such as `<strong>` or `<a>`, the text before and after those tags becomes separate nodes. Both `factcheck_exports/{post_id}.json` (exported to Claude) and `sessions/factcheck/{post_id}.json` (written back by Claude) use this format, and the blog-factcheck skill depends on it, so it was left unchanged.

### What Is "Rate Limiting"

Gemini's free tier has three quotas, each counted independently:

| Name                       | Meaning                                                                                                  |
| -------------------------- | -------------------------------------------------------------------------------------------------------- |
| RPM (Requests Per Minute)  | Maximum API calls per minute                                                                             |
| TPM (Tokens Per Minute)    | Maximum tokens processed per minute; usually a large quota that's rarely hit                             |
| RPD (Requests Per Day)     | Maximum API calls per day, shared by all usage under this API project (across all posts); often the first one hit |

RPD is especially easy to hit first because it counts the "number of requests", not the "amount of content" — even if you pack a lot of text into one call, it still counts as one request.

### Current Measures

1. **Section-based requests (saves RPM / RPD)**: One API call per H2 section, so the request count equals the number of sections and doesn't grow with the amount of bold text or links in the layout. (The old `CORRECTION_BATCH_SIZE` setting is no longer used.)
2. **Per-minute throttling**:
    - Each "Start / Re-run Proofreading" in `app.py` makes at most `GEMINI_RPM_LIMIT` (default 15) API calls (including retries after failed format checks), then stops, saves progress, and waits for the user to click again to continue.
    - `_throttle()` in `ai_corrector.py` is the last line of defense, ensuring no more than `GEMINI_RPM_LIMIT` requests are sent within the past 60 seconds.
3. **Automatic 429 retries**: When `_generate_with_retry` in `ai_corrector.py` hits a 429 (quota exceeded), it reads the suggested wait time returned by Gemini and retries up to `GEMINI_MAX_RETRIES` (default 3) times. **This mechanism is designed for hitting RPM** — if RPD (the daily quota) is used up, retrying won't help and you'll have to wait until the quota resets the next day. In that case, the on-screen "please wait a moment and try again" message is misleading.
4. **Output limit**: `SECTION_MAX_OUTPUT_TOKENS` (default 12000) in `ai_corrector.py` caps how many tokens a single section response can return. Section proofreading returns the full corrected text of the section; if the response is ever truncated, the JSON fails to parse and the section shows "AI processing failed, original kept" — a half-finished result is never applied.
