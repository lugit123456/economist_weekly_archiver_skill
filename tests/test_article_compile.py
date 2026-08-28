from __future__ import annotations

import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sync_weekly import (
    DEFAULTS,
    _parse_next_article_content,
    _summary_length_bounds,
    compile_article_record,
    materialize_image_placements,
)


def _config(*, max_retries: int = 0) -> dict[str, object]:
    config = {key: dict(value) for key, value in DEFAULTS.items()}
    config["crawl"]["max_retries"] = max_retries  # type: ignore[index]
    config["glossary"]["enabled"] = False  # type: ignore[index]
    return config


def _natural_summary() -> str:
    sentence = "文章围绕核心问题展开分析，并用关键事实解释相关风险以及政策选择。"
    paragraph = sentence * 5
    return "\n\n".join([paragraph, paragraph, paragraph])


class _FakeCompletions:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = payloads
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.requests.append(kwargs)
        payload = self.payloads[len(self.requests) - 1]
        message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _client(payloads: list[dict[str, object]]) -> tuple[SimpleNamespace, _FakeCompletions]:
    completions = _FakeCompletions(payloads)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


class ArticleCompileTests(unittest.TestCase):
    def test_translation_and_summary_use_separate_requests(self) -> None:
        summary = _natural_summary()
        client, completions = _client([
            {
                "paragraphs": [
                    {"zh_text": "第一段自然译文。", "role": "crosshead"},
                    {"zh_text": "第二段自然译文。", "role": "body"},
                ],
            },
            {"title_zh": "自然中文标题", "summary_md": summary},
        ])

        article = compile_article_record(
            client,
            _config(),
            issue_date="2026-08-08",
            section="Leaders",
            title="A test article",
            url="https://example.com/article",
            body="First source paragraph.\n\nSecond source paragraph.",
            article_id="art_test_001",
            log_=logging.getLogger("test"),
            images=["images/example.jpg"],
            image_placements=[{"path": "images/example.jpg", "after_paragraph": 1}],
        )

        self.assertIsNotNone(article)
        assert article is not None
        self.assertEqual(len(completions.requests), 2)
        translation_prompt = completions.requests[0]["messages"][1]["content"]  # type: ignore[index]
        summary_prompt = completions.requests[1]["messages"][1]["content"]  # type: ignore[index]
        self.assertIn("不写摘要", translation_prompt)
        self.assertIn("不是专名", translation_prompt)
        self.assertIn("不机械写成", translation_prompt)
        self.assertIn("不翻译、不音译，也不改用中文别名", translation_prompt)
        self.assertIn("Google、Reddit、Instagram、TikTok、Sensor Tower", translation_prompt)
        self.assertNotIn('"summary_md"', translation_prompt)
        self.assertIn("这不是逐段翻译", summary_prompt)
        self.assertIn("政治和外交语境", summary_prompt)
        self.assertIn("不翻译、不音译，也不改用中文别名", summary_prompt)
        self.assertIn("Google、Reddit、Instagram、TikTok、Sensor Tower", summary_prompt)
        self.assertNotIn('"paragraphs":', summary_prompt)
        self.assertEqual(article["title_zh"], "自然中文标题")
        self.assertEqual(article["summary_md"], summary)
        self.assertEqual([item["role"] for item in article["paragraphs"]], ["body", "body"])
        self.assertEqual(
            article["image_placements"],
            [{"path": "images/example.jpg", "after_paragraph": 1}],
        )
        self.assertTrue(article["compiled_article"])
        self.assertEqual(article["compile_status"], "complete")
        self.assertEqual(
            set(article),
            {
                "id", "issue_date", "section", "title", "title_zh", "url",
                "summary_md", "content_raw", "content_markdown", "paragraphs",
                "images", "image_placements", "image_insights", "compiled_article", "compile_status",
                "glossary_entries", "term_annotations", "glossary_analysis_complete",
                "glossary_version",
            },
        )

    def test_next_data_images_keep_their_paragraph_positions(self) -> None:
        lead_url = "https://www.economist.com/content-assets/images/lead.jpg"
        chart_url = "https://www.economist.com/content-assets/images/chart.png"
        title, body, image_urls, placements = _parse_next_article_content({
            "headline": "Positioned images",
            "leadComponent": {"url": lead_url},
            "body": [
                {"type": "PARAGRAPH", "text": "First paragraph."},
                {"type": "PARAGRAPH", "text": "Second paragraph."},
                {"type": "IMAGE", "url": chart_url},
                {"type": "CROSSHEAD", "text": "What comes next"},
                {"type": "PARAGRAPH", "text": "Third paragraph."},
            ],
        })

        self.assertEqual(title, "Positioned images")
        self.assertEqual(
            body,
            "First paragraph.\n\nSecond paragraph.\n\n\n## What comes next\n\n\nThird paragraph.",
        )
        self.assertEqual(image_urls, [lead_url, chart_url])
        self.assertEqual(placements, [
            {"url": lead_url, "after_paragraph": 0},
            {"url": chart_url, "after_paragraph": 2},
        ])

    def test_materialized_image_paths_preserve_positions(self) -> None:
        image_urls = ["https://example.com/lead.jpg", "https://example.com/chart.png"]
        placements = [
            {"url": image_urls[0], "after_paragraph": 0},
            {"url": image_urls[1], "after_paragraph": 2},
        ]

        self.assertEqual(
            materialize_image_placements(
                image_urls,
                ["images/article_01.jpg", "images/article_02.png"],
                placements,
            ),
            [
                {"path": "images/article_01.jpg", "after_paragraph": 0},
                {"path": "images/article_02.png", "after_paragraph": 2},
            ],
        )

    def test_translation_validation_retries_without_repeating_summary(self) -> None:
        client, completions = _client([
            {"paragraphs": []},
            {"paragraphs": [{"zh_text": "完整译文。", "role": "body"}]},
            {"title_zh": "中文标题", "summary_md": _natural_summary()},
        ])

        with patch("sync_weekly.time.sleep", return_value=None):
            article = compile_article_record(
                client,
                _config(max_retries=1),
                issue_date="2026-08-08",
                section="Leaders",
                title="Retry translation",
                url="https://example.com/retry",
                body="Only one source paragraph.",
                article_id="art_test_002",
                log_=logging.getLogger("test"),
            )

        self.assertIsNotNone(article)
        self.assertEqual(len(completions.requests), 3)
        self.assertIn("忠实翻译全文", completions.requests[0]["messages"][0]["content"])  # type: ignore[index]
        self.assertIn("忠实翻译全文", completions.requests[1]["messages"][0]["content"])  # type: ignore[index]
        self.assertIn("原创编辑稿", completions.requests[2]["messages"][0]["content"])  # type: ignore[index]

    def test_summary_length_expands_with_source_size(self) -> None:
        self.assertEqual(
            _summary_length_bounds([{"en_text": "word " * 900}]),
            (420, 650),
        )
        self.assertEqual(
            _summary_length_bounds([{"en_text": "word " * 901}]),
            (520, 800),
        )
        self.assertEqual(
            _summary_length_bounds([{"en_text": "word " * 1801}]),
            (620, 1000),
        )


if __name__ == "__main__":
    unittest.main()
