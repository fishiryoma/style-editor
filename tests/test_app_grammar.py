"""日文校對 API 流程測試（Gemini 與資料庫都用假的，不會動到真的文章或草稿）。
執行：python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module  # noqa: E402
import section_diff as sd  # noqa: E402
from test_section_diff import ARTICLE  # noqa: E402

POST_ID = 424242


def fake_correct_section(client, model, payload, mode, instruction=None, previous=None):
    """輕修：把「美味しいでした」改成「美味しかったです」；重寫：每個群組的段落順序倒過來合併成一段。"""
    items, runs = {}, {}
    for el in payload:
        if el.get("type") != "paragraphs":
            continue
        paras = el["paragraphs"]
        if mode == "light":
            fixed = [p.replace("美味しいでした", "美味しかったです") for p in paras]
            if fixed != paras:
                runs[el["id"]] = fixed
        elif len(paras) > 1:
            runs[el["id"]] = ["".join(reversed(paras))]
    note = "調整段落順序" if mode == "rewrite" else ""
    if instruction:
        note = "依意見調整：" + instruction
    return {"items": items, "runs": runs, "note": note}


class GrammarFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "grammar").mkdir()
        (tmp / "factcheck").mkdir()
        (tmp / "backups").mkdir()
        self.post = {"id": POST_ID, "title": "テスト記事", "slug": "test", "content": ARTICLE}
        self.updated = {}
        patches = [
            mock.patch.object(app_module, "GRAMMAR_SESS_DIR", tmp / "grammar"),
            mock.patch.object(app_module, "FACTCHECK_SESS_DIR", tmp / "factcheck"),
            mock.patch.object(app_module, "BACKUP_DIR", tmp / "backups"),
            mock.patch.object(app_module, "get_client", return_value=object()),
            mock.patch.object(app_module.wp_db, "get_post", side_effect=lambda pid: dict(self.post)),
            mock.patch.object(app_module.wp_db, "update_post", side_effect=self._fake_update),
            mock.patch.object(app_module.ai_corrector, "correct_section", side_effect=fake_correct_section),
            mock.patch.object(
                app_module.ai_corrector,
                "suggest_title",
                return_value={"grammar_ok": True, "note": "OK", "suggestions": []},
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.client = app_module.app.test_client()
        self.tmp_path = tmp

    def _fake_update(self, post_id, content, title=None):
        self.updated = {"content": content, "title": title}

    def _prepare(self, mode):
        return self.client.post(f"/api/posts/{POST_ID}/grammar/prepare", json={"force": True, "mode": mode})

    def test_light_flow_and_apply(self):
        r = self._prepare("light")
        self.assertEqual(r.status_code, 200, r.json)
        data = r.json
        self.assertEqual(data["mode"], "light")
        self.assertEqual(data["summary"]["sections"], 3)
        self.assertEqual(data["summary"]["total_changes"], 1)
        self.assertIn("sec-card", data["review_html"])

        r = self.client.post(f"/api/posts/{POST_ID}/grammar/apply")
        self.assertEqual(r.status_code, 200, r.json)
        expected = ARTICLE.replace("<p>美味しいでした。</p>", "<p>美味しかったです。</p>")
        self.assertNotEqual(expected, ARTICLE)
        self.assertEqual(self.updated["content"], expected)  # 只改到那一句，其他一個字元都沒變
        self.assertEqual(len(list((self.tmp_path / "backups").glob("*.json"))), 1)

    def test_rewrite_flow_reject_and_switch(self):
        r = self._prepare("rewrite")
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json["summary"]["total_changes"], 1)
        self.assertIn("sec-compare", r.json["review_html"])

        r = self.client.post(f"/api/posts/{POST_ID}/grammar/section/status", json={"section_index": 1, "status": "rejected"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json["summary"]["rejected"], 1)
        self.assertIn("is-rejected", r.json["card_html"])

        # 第 1 段改用輕修重跑
        r = self.client.post(f"/api/posts/{POST_ID}/grammar/redo", json={"section_index": 1, "mode": "light"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertIn("mode-light", r.json["card_html"])
        self.assertIn("is-accepted", r.json["card_html"])

        # 帶意見重新調整
        r = self.client.post(
            f"/api/posts/{POST_ID}/grammar/redo", json={"section_index": 1, "instruction": "もっと短く"}
        )
        self.assertEqual(r.status_code, 200, r.json)
        self.assertIn("依意見調整", r.json["card_html"])

        r = self.client.post(f"/api/posts/{POST_ID}/grammar/apply")
        self.assertEqual(r.status_code, 200, r.json)
        self.assertIn("美味しかったです。", self.updated["content"])

    def test_legacy_session_returns_410(self):
        (self.tmp_path / "grammar" / f"{POST_ID}.json").write_text(
            json.dumps({"post_id": POST_ID, "title": "t", "nodes": [], "raw_original_content": ARTICLE}),
            encoding="utf-8",
        )
        r = self.client.get(f"/api/posts/{POST_ID}/grammar/review")
        self.assertEqual(r.status_code, 410)
        # 舊版草稿可以直接重新校對
        r = self._prepare("light")
        self.assertEqual(r.status_code, 200, r.json)

    def test_ai_failure_keeps_original(self):
        with mock.patch.object(app_module.ai_corrector, "correct_section", side_effect=ValueError("截斷")):
            r = self._prepare("light")
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json["summary"]["failed"], 3)
        self.assertTrue(r.json["warnings"])
        r = self.client.post(f"/api/posts/{POST_ID}/grammar/apply")
        self.assertEqual(self.updated["content"], ARTICLE)

    def test_batch_delete(self):
        for kind, pid in [("grammar", "11"), ("grammar", "12"), ("factcheck", "11")]:
            (self.tmp_path / kind / f"{pid}.json").write_text("{}", encoding="utf-8")
        r = self.client.post(
            "/api/sessions/delete",
            json={
                "items": [
                    {"kind": "grammar", "post_id": "11"},
                    {"kind": "factcheck", "post_id": "11"},
                    {"kind": "grammar", "post_id": "../12"},
                    {"kind": "other", "post_id": "12"},
                ]
            },
        )
        self.assertEqual(r.json, {"ok": True, "deleted": 2, "skipped": 2})
        self.assertTrue((self.tmp_path / "grammar" / "12.json").exists())
        self.assertFalse((self.tmp_path / "grammar" / "11.json").exists())
        self.assertEqual(self.client.post("/api/sessions/delete", json={"items": []}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
