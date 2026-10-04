# Japanese Proofreading Tool (style-editor)

[繁體中文](README.md) | **English**

A full-stack local tool that reads directly from the **Local WP (local WordPress development environment) MySQL database**. It uses the **Gemini API** for paragraph-by-paragraph Japanese proofreading and a **Claude Code skill** for fact-checking, and tracks review progress. Each post is split into HTML text nodes, and the AI suggests edits node by node. Nothing is written back to the database until you've reviewed every suggestion ("accept / keep original / ask for a rewrite"). The original is backed up automatically before it's overwritten, so every change stays under your control and can be rolled back.

> **Why I built this**: I'm a non-native speaker learning Japanese, and I still write my blog about daily life and travel in Japanese. Most AI proofreading tools "correct" text into textbook-standard Japanese, erasing the nuance, casual tone, and kaomoji that come naturally when a Taiwanese writer writes in Japanese. So I built this tool to have the AI flag only genuine grammar and word-choice errors while keeping my own writing style intact.

## Three Modes

The web page has three tabs on the left. Each one works independently:

![style-editor screenshot](./images/preview.png)

### 📝 Japanese Proofreading

Uses Gemini to proofread Japanese according to your writing style and to check/suggest titles. Each edit is marked inline on the page, and the original post in the WordPress database is only overwritten after you've reviewed everything.

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

Lists the progress of all drafts created by proofreading and fact-checking (of either kind). You can see each draft's status and timestamp, or delete drafts you no longer need. Deleting here only removes the local review progress files; it never touches the posts in the WordPress database.

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

**Current model**: `gemini-3.5-flash-lite` (switched in September 2026; see the background in "Nodes and Rate Limits" below — its free quota is considerably larger than `gemini-3.5-flash`'s).
If you later hit errors because the free quota is used up, you can change `GEMINI_MODEL` in `.env` to another model (e.g. the older `gemini-3.1-flash-lite`).
Free-tier quotas may change over time; check your account's current quota at https://aistudio.google.com/rate-limit.

The DB connection settings are read from the same `.env` (shared with `sync-analytics.py`), so there's nothing else to configure.

## Running

```powershell
cd "<your-blog-project-folder>\style-editor"
python app.py
```

Open http://localhost:5055 in your browser.

## Japanese Proofreading: Step by Step

1. Select a post from the list on the left.
2. Click "Start / Re-run AI Proofreading". The AI does two things at once (longer posts take longer):
    - Proofreads the body text
    - Reads the whole post, then checks the title and suggests alternatives if appropriate
3. **Title section**: The title grammar check result appears below the title. If there are suggestions, you'll see "Keep original title" plus up to 3 new titles; pick the one you want with the radio buttons (defaults to keeping the original).
4. **Body section**: Green underlines are suggested edits; red strikethrough is the original text. Hover over an edit to see three small buttons:
    - ✓ Accept this edit
    - ✕ Keep the original, don't accept
    - ✎ Not happy with it — enter feedback and ask the AI to redo this passage (e.g. "this sounds too stiff", "this isn't a typo")
    - Every edit is "accepted" by default, so you only need to handle the ones you disagree with.
5. Once everything looks good, click "Apply and Overwrite Original" in the top right. The post content (and your chosen title, if it isn't the original) is overwritten, and the original version is automatically backed up to `style-editor/backups/`.
6. You can close the browser at any time. When you reopen the page and select the same post, your progress is still there (stored in `style-editor/sessions/`; nothing affects WordPress until you apply).

## Restoring a Backup

`style-editor/backups/{post ID}-{slug}-{timestamp}.json` contains the original `{title, content}` from before the overwrite.
To restore, open phpMyAdmin / Adminer in Local WP and paste the backup's contents back into the post's `wp_posts.post_title` / `post_content` columns.

## Known Limitations

- "✎ Redo" re-proofreads the entire passage (the same text node), not just a single word. If the passage contains multiple edits, they are all regenerated together.
- Only body text nodes and the title are processed. SEO meta, category names, and other fields are not proofread.
- There's no user login. It's meant only for your own local use — don't expose this web page publicly (e.g. don't publish it with tools like ngrok).
- Gemini's free quota is limited (varies by model, with caps on requests per minute and requests per day). Long posts, or proofreading several posts in quick succession, may hit the limit; if you see an error message, wait a moment and try again. See "Nodes and Rate Limits" below for details.

## Nodes and Rate Limits (Technical Notes)

### Background: Why These Measures Exist

In September 2026, the model in use was `gemini-3.5-flash`, with a free quota of **RPM 5, TPM 250K, RPD 20** (live numbers from
https://aistudio.google.com/rate-limit). RPD (requests per day) was only 20 and shared across the entire
API project, so it was hit quickly in practice. The batching, per-minute throttling, 429 retries, and output limits listed
under "Current Measures" below were all designed around that quota.

After switching to `gemini-3.5-flash-lite` on September 12, 2026, the same account's free quota became **RPM 15, TPM 250K,
RPD 500** — RPD went from 20 to 500 (25×) and RPM from 5 to 15 (3×). The RPD bottleneck that used to be hit first
should no longer come up in normal use. The default for `GEMINI_RPM_LIMIT` has been updated to 15 accordingly; batching and 429 retries
were **kept** as safeguards (the quota is still finite, just more generous).

### What Is a "Node"

This project doesn't send the whole post to Gemini as one big block of text. Instead, `extract_nodes` in `html_diff.py` first splits the post's HTML into individual "nodes". **A node is a single run of continuous text (an HTML text node), not an entire paragraph**: if a `<p>` paragraph contains inline tags such as `<strong>` or `<a>`, the text before and after those tags is split into separate nodes rather than merged into one. Before being sent to the AI, each node is wrapped in a tag like `⟦N0⟧...⟦/N0⟧` so the AI's response can be mapped back by its original number.

Benefits of splitting this way:

- Status is tracked per passage (`corrected: True`/`False`), so if the run is interrupted or hits a rate limit, passages that were already proofread don't need to be redone.
- On the page you can "accept / keep original / redo" a single passage, instead of applying or redoing the entire post all at once.

### Example: How Many Nodes and Requests for a 3,000-Character Post

Because nodes are split by "text node" (see above), **the node count depends mainly on how many inline tags (`<strong>`, `<a>`, etc.) the layout uses, not simply on character count**. For the same 3,000 characters, the busier the formatting, the more nodes. Two scenarios:

- **Almost no inline tags** (e.g. 3 `<h2>` subheadings + 10 `<p>` paragraphs + 1 four-item list, with continuous text in each paragraph and no bold/links mixed in): about **17 nodes** (3 + 10 + 4).
- **Typical Cocoon blog formatting** (paragraphs interspersed with bold emphasis and internal links, each paragraph split into 1.5–2 text nodes on average): the same structure may become **25–35 nodes**.

Converting to request count using `CORRECTION_BATCH_SIZE` (default 12) (`ceil(nodes / 12)`, plus 1 for title suggestions):

| Nodes | Body proofreading batches | + Title suggestions | Total requests  |
| ----- | ------------------------- | ------------------- | --------------- |
| 17    | ceil(17/12) = 2           | +1                  | **3 requests**  |
| 30    | ceil(30/12) = 3           | +1                  | **4 requests**  |

Both cases are well below `GEMINI_RPM_LIMIT` (default 15), so normally one click of "Start / Re-run" processes the whole post without entering the resume flow ("X/Y passages done, please wait and click again"). You'll only need multiple clicks for especially long posts (around a hundred nodes or more), or when part of the day's quota has already been used by other posts.

### What Is "Rate Limiting"

Gemini's free tier has three quotas, each counted independently:

| Name                       | Meaning                                                                                                  |
| -------------------------- | -------------------------------------------------------------------------------------------------------- |
| RPM (Requests Per Minute)  | Maximum API calls per minute                                                                             |
| TPM (Tokens Per Minute)    | Maximum tokens processed per minute; usually a large quota that's rarely hit                             |
| RPD (Requests Per Day)     | Maximum API calls per day, shared by all usage under this API project (across all posts); often the first one hit |

RPD is especially easy to hit first because it counts the "number of requests", not the "amount of content" — even if you pack a lot of text into one call, it still counts as one request.

### Current Measures

1. **Batching (saves RPM / RPD)**: Instead of one API call per node, `CORRECTION_BATCH_SIZE` (`.env`, default 12) nodes are packed into a batch and proofread in a single API call. For a fixed number of nodes, larger batches mean fewer total requests.
2. **Per-minute throttling**:
    - Each "Start / Re-run Proofreading" in `app.py` sends at most `GEMINI_RPM_LIMIT` (default 15) batches, then stops, saves progress, and waits for the user to click again to continue.
    - `_throttle()` in `ai_corrector.py` is the last line of defense, ensuring no more than `GEMINI_RPM_LIMIT` requests are sent within the past 60 seconds.
3. **Automatic 429 retries**: When `_generate_with_retry` in `ai_corrector.py` hits a 429 (quota exceeded), it reads the suggested wait time returned by Gemini and retries up to `GEMINI_MAX_RETRIES` (default 3) times. **This mechanism is designed for hitting RPM** — if RPD (the daily quota) is used up, retrying won't help and you'll have to wait until the quota resets the next day. In that case, the on-screen "please wait a moment and try again" message is misleading.
4. **Output limit tied to batch size**: `BATCH_MAX_OUTPUT_TOKENS` (default 8000) in `ai_corrector.py` caps how many tokens a single batch proofreading response can return. Because batch proofreading echoes back **the full corrected text of every node in the batch** (not just the diffs), if you increase `CORRECTION_BATCH_SIZE` and the batch's text exceeds this limit, the AI's response gets truncated. Truncated nodes are treated as "unchanged" and keep their original text — no error is shown on the page; the passage just looks like it wasn't edited. When you increase the batch size, raise this constant as well.
