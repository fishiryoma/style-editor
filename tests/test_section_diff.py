"""section_diff 的單元測試。執行：python -m unittest discover -s tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import section_diff as sd  # noqa: E402

P = "<!-- wp:paragraph -->\n<p>{}</p>\n<!-- /wp:paragraph -->"
H2 = '<!-- wp:heading -->\n<h2 class="wp-block-heading">{}</h2>\n<!-- /wp:heading -->'
H3 = '<!-- wp:heading {{"level":3}} -->\n<h3 class="wp-block-heading">{}</h3>\n<!-- /wp:heading -->'
IMG = (
    '<!-- wp:image {"id":1} -->\n<figure class="wp-block-image"><img src="a.jpg" alt=""/>'
    '<figcaption class="wp-element-caption">写真の説明です</figcaption></figure>\n<!-- /wp:image -->'
)

ARTICLE = "\n\n".join(
    [
        P.format("はじめにの文です。"),
        H2.format("辛いまぜそば"),
        P.format("名前は<strong>「愛恨椒芝麵」</strong>です。"),
        P.format("値段は<span class=\"bold-red\"><span class=\"fz-20px\">70元</span></span>でした。"),
        IMG,
        P.format("美味しいでした。"),
        H3.format("小見出しです"),
        P.format("小見出しの下の文です。"),
        H2.format("牡蠣"),
        '<!-- wp:paragraph {"extraStyle":"cross-line"} -->\n<p class="is-style-box">装飾のある段落です。</p>\n<!-- /wp:paragraph -->',
        P.format("牡蠣が美味しかったです。"),
    ]
)


def ids_of_runs(section):
    return [u for u in section["units"] if u["type"] == "run"]


class SplitTests(unittest.TestCase):
    def test_sections_concat_to_original(self):
        secs = sd.parse_sections(ARTICLE)
        self.assertEqual("".join(sd.section_original_html(s) for s in secs), ARTICLE)

    def test_split_by_h2(self):
        secs = sd.parse_sections(ARTICLE)
        self.assertEqual([s["heading"] for s in secs], ["（開頭）", "辛いまぜそば", "牡蠣"])

    def test_article_starting_with_h2_has_no_empty_intro(self):
        secs = sd.parse_sections("\n" + H2.format("最初") + "\n\n" + P.format("本文です。"))
        self.assertEqual(len(secs), 1)
        self.assertEqual(secs[0]["heading"], "最初")

    def test_image_and_h3_split_runs(self):
        sec = sd.parse_sections(ARTICLE)[1]
        runs = ids_of_runs(sec)
        # 圖片前 2 段、圖片後 1 段、H3 後 1 段 → 三個群組
        self.assertEqual([len(r["para_ids"]) for r in runs], [2, 1, 1])

    def test_styled_paragraph_is_not_in_run(self):
        sec = sd.parse_sections(ARTICLE)[2]
        kinds = [b["kind"] for b in sec["blocks"] if b["kind"] != "gap"]
        self.assertEqual(kinds, ["heading", "styled", "para"])

    def test_tokens_roundtrip(self):
        for sec in sd.parse_sections(ARTICLE):
            for b in sec["blocks"]:
                for iid in b["items"]:
                    it = sec["items"][iid]
                    s, e = it["span"]
                    self.assertEqual(sd.detokenize(it["text"], it["tokens"]), b["raw"][s:e])

    def test_caption_is_light_item(self):
        sec = sd.parse_sections(ARTICLE)[1]
        texts = [it["text"] for it in sec["items"].values() if it["type"] == "text"]
        self.assertIn("写真の説明です", texts)

    def test_text_in_block_attrs_is_skipped(self):
        content = (
            '<!-- wp:cocoon-blocks/button-1 {"content":"詳しくはこちら"} -->\n'
            '<div class="btn-wrap"><a href="x">詳しくはこちら</a></div>\n<!-- /wp:cocoon-blocks/button-1 -->'
        )
        sec = sd.parse_sections(content)[0]
        self.assertEqual(sec["items"], {})

    def test_malformed_inline_falls_back_to_fixed(self):
        content = P.format("壊れた<strong>タグです")
        sec = sd.parse_sections(content)[0]
        self.assertEqual(sec["blocks"][0]["kind"], "fixed")
        self.assertEqual("".join(sd.section_original_html(s) for s in sd.parse_sections(content)), content)


class ValidateAndRebuildTests(unittest.TestCase):
    def setUp(self):
        self.sec = sd.parse_sections(ARTICLE)[1]
        self.run0 = ids_of_runs(self.sec)[0]
        self.p0, self.p1 = self.run0["para_ids"]
        self.t0 = self.sec["items"][self.p0]["text"]
        self.t1 = self.sec["items"][self.p1]["text"]

    def test_light_change_rebuild(self):
        last_run = ids_of_runs(self.sec)[1]
        pid = last_run["para_ids"][0]
        parsed = {"items": {}, "runs": {last_run["id"]: ["美味しかったです。"]}, "note": ""}
        result, problems = sd.validate_result(self.sec, parsed, "light")
        self.assertEqual(problems, [])
        self.assertEqual(result["items"], {pid: "美味しかったです。"})
        out = sd.rebuild_section(self.sec, result)
        self.assertIn("<p>美味しかったです。</p>", out)
        self.assertEqual(out.replace("美味しかったです。", "美味しいでした。"), sd.section_original_html(self.sec))

    def test_light_rejects_paragraph_count_change(self):
        parsed = {"runs": {self.run0["id"]: [self.t0 + self.t1]}}
        result, problems = sd.validate_result(self.sec, parsed, "light")
        self.assertTrue(problems)
        self.assertFalse(sd.result_has_changes(result))

    def test_rewrite_merges_paragraphs_and_moves_tokens(self):
        merged = self.t1 + "そして" + self.t0
        parsed = {"runs": {self.run0["id"]: [merged]}, "note": "把價格移到前面"}
        result, problems = sd.validate_result(self.sec, parsed, "rewrite")
        self.assertEqual(problems, [])
        out = sd.rebuild_section(self.sec, result)
        self.assertIn(
            '<p>値段は<span class="bold-red"><span class="fz-20px">70元</span></span>でした。'
            "そして名前は<strong>「愛恨椒芝麵」</strong>です。</p>",
            out,
        )
        self.assertEqual(out.count("<!-- wp:paragraph -->"), sd.section_original_html(self.sec).count("<!-- wp:paragraph -->") - 1)
        self.assertIn(IMG, out)  # 圖片原樣保留

    def test_rewrite_rejects_missing_token(self):
        broken = self.t0.replace("⟦/T1⟧", "")
        parsed = {"runs": {self.run0["id"]: [broken, self.t1]}}
        _, problems = sd.validate_result(self.sec, parsed, "rewrite")
        self.assertTrue(problems)

    def test_rewrite_rejects_token_split_across_paragraphs(self):
        a, b = self.t0.split("⟦/T1⟧")
        parsed = {"runs": {self.run0["id"]: [a, "⟦/T1⟧" + b, self.t1]}}
        _, problems = sd.validate_result(self.sec, parsed, "rewrite")
        self.assertTrue(problems)

    def test_split_sentences_concat(self):
        text = "なんと⟦T1⟧70元！！普通⟦/T1⟧です。「すごい！」と言った\n最後"
        parts = sd.split_sentences(text)
        self.assertEqual("".join(parts), text)
        self.assertEqual(parts[0], "なんと⟦T1⟧70元！！")

    def test_changed_sentences_marks_moved_and_edited(self):
        orig = ["Aです。Bです。", "Cです。"]
        new = ["Bです。Aです。Cでした。"]
        a, b = sd.changed_sentences(orig, new)
        # A と B の入れ替えは片方が「移動した文」として標示され、C は修正なので必ず標示される
        self.assertEqual(len(a[0]), 1)
        self.assertEqual(a[1], {0})
        self.assertEqual(len(b[0]), 2)
        self.assertIn(2, b[0])

    def test_rewrite_side_highlight_keeps_valid_nesting(self):
        # 太字が「値段は…でした。」の文をまたいでいても、<mark> は標籤の内側だけを包む
        merged = self.t1 + "そして" + self.t0
        result, _ = sd.validate_result(self.sec, {"runs": {self.run0["id"]: [merged]}}, "rewrite")
        new_html = sd.rewrite_side_html(self.sec, result, "new")
        orig_html = sd.rewrite_side_html(self.sec, result, "orig")
        self.assertIn('<strong><mark class="sec-hl">「愛恨椒芝麵」</mark></strong>', new_html)
        self.assertIn('<mark class="sec-hl">', orig_html)
        self.assertIn(IMG, orig_html)  # 圖片照原樣顯示
        # 沒改到的段落不標
        self.assertIn("<p>美味しいでした。</p>", new_html)

    def test_symbol_forms_restored(self):
        self.assertEqual(sd.restore_symbol_forms("2～3口...無理", "2〜3口…無理"), "2～3口...無理")
        # 原文本來就混用兩種寫法時不動
        self.assertEqual(sd.restore_symbol_forms("2～3と4〜5", "2〜3と4〜5"), "2〜3と4〜5")

    def test_rejects_raw_html(self):
        parsed = {"runs": {self.run0["id"]: [self.t0, self.t1 + "<b>x</b>"]}}
        _, problems = sd.validate_result(self.sec, parsed, "rewrite")
        self.assertTrue(problems)

    def test_build_final_respects_status(self):
        secs = sd.parse_sections(ARTICLE)
        last_run = ids_of_runs(secs[1])[1]
        result, _ = sd.validate_result(secs[1], {"runs": {last_run["id"]: ["美味しかったです。"]}}, "light")
        recs = [None, {"status": "accepted", "result": result}, None]
        self.assertIn("美味しかったです。", sd.build_final_content(ARTICLE, recs))
        recs[1]["status"] = "rejected"
        self.assertEqual(sd.build_final_content(ARTICLE, recs), ARTICLE)

    def test_render_card_light_and_rewrite(self):
        last_run = ids_of_runs(self.sec)[1]
        result, _ = sd.validate_result(self.sec, {"runs": {last_run["id"]: ["美味しかったです。"]}}, "light")
        html = sd.render_section_card(self.sec, {"mode": "light", "status": "accepted", "corrected": True, "result": result})
        self.assertIn("tp-del", html)
        self.assertIn('data-act="accept"', html)
        result, _ = sd.validate_result(self.sec, {"runs": {self.run0["id"]: [self.t1, self.t0]}, "note": "調換順序"}, "rewrite")
        html = sd.render_section_card(self.sec, {"mode": "rewrite", "status": "accepted", "corrected": True, "result": result})
        self.assertIn("sec-compare", html)
        self.assertIn("調換順序", html)


if __name__ == "__main__":
    unittest.main()
