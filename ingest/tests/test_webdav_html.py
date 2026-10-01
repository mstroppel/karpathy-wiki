import unittest

from karpathy_wiki_ingest.shared import PrivacyValidationError, TargetedAnonymizer
from karpathy_wiki_ingest_webdav.html import decoded_view, redact_html


class HtmlRedactionTests(unittest.TestCase):
    def setUp(self):
        self.anonymizer = TargetedAnonymizer.from_config(
            {
                "people": [{"values": ["Max Mustermann"], "replacement": "[PERSON]"}],
                "emails": [{"values": ["max@example.test"], "replacement": "[EMAIL]"}],
                "phones": [{"values": ["+49 123 456789"], "replacement": "[PHONE]"}],
            }
        )

    def redact(self, source):
        return redact_html(source, self.anonymizer)

    def test_no_match_preserves_exact_source(self):
        source = '<!DOCTYPE html>\r\n<P CLASS="x" data-a=ok>Safe &amp; &#169;</P>\r\n'
        self.assertEqual(self.redact(source), source)

    def test_entity_decoding_and_inline_markup(self):
        for source, expected in (
            ("<p>Max&#32;Mustermann</p>", "<p>[PERSON]</p>"),
            ("<p>Ma&#x78; <b>Mustermann</b>!</p>", "<p>[PERSON]<b></b>!</p>"),
            ("<p>Max&nbsp;<em>Muster</em>mann</p>", "<p>[PERSON]<em></em></p>"),
            ("<p>Max <!-- note -->Mustermann</p>", "<p>[PERSON]<!-- note --></p>"),
            ("<p>max&#64;example.test</p>", "<p>[EMAIL]</p>"),
            ("<p>+49 <b>123</b> 456789</p>", "<p>[PHONE]<b></b></p>"),
            ("<p>Max<br/>Mustermann</p>", "<p>[PERSON]<br/></p>"),
        ):
            with self.subTest(source=source):
                self.assertEqual(self.redact(source), expected)

    def test_attributes_comments_and_active_content_are_redacted_not_removed(self):
        source = (
            "<a href=\"mailto:max&#64;example.test\" title='Max&nbsp;Mustermann' "
            "data-email=max@example.test>link</a>"
            "<!-- Max Mustermann -->"
            '<script>const name = "Max Mustermann";</script>'
            "<style>/* Max Mustermann */</style>"
        )
        expected = (
            "<a href=\"mailto:[EMAIL]\" title='[PERSON]' data-email=[EMAIL]>link</a>"
            "<!-- [PERSON] -->"
            '<script>const name = "[PERSON]";</script>'
            "<style>/* [PERSON] */</style>"
        )
        self.assertEqual(self.redact(source), expected)

    def test_case_insensitive_repeated_matches(self):
        self.assertEqual(
            self.redact("<p>MAX <b>MUSTERMANN</b>; Max Mustermann</p>"),
            "<p>[PERSON]<b></b>; [PERSON]</p>",
        )

    def test_full_structured_name_takes_precedence_over_raw_fragments(self):
        anonymizer = TargetedAnonymizer.from_config(
            {
                "people": [
                    {"first_name": "Max", "last_name": "Mustermann", "replacement": "[PERSON]"}
                ]
            }
        )
        source = '<p title="Max&#32;Mustermann">Max <b>Mustermann</b></p>'
        self.assertEqual(redact_html(source, anonymizer), '<p title="[PERSON]">[PERSON]<b></b></p>')

    def test_matching_tag_name_fails_closed(self):
        anonymizer = TargetedAnonymizer.from_config(
            {"people": [{"values": ["mark"], "replacement": "[PERSON]"}]}
        )
        with self.assertRaises(PrivacyValidationError):
            redact_html("<mark>safe</mark>", anonymizer)

    def test_post_redaction_validation_fails_closed(self):
        anonymizer = TargetedAnonymizer.from_config(
            {"people": [{"values": ["PERSON"], "replacement": "[PERSON]"}]}
        )
        with self.assertRaises(PrivacyValidationError):
            redact_html("<p>PERSON</p>", anonymizer)

    def test_optional_semicolons_unknown_and_multicharacter_entities(self):
        text, spans = decoded_view("&notit; &unknown; &#65 &#x42; &NotEqualTilde;", 10)
        self.assertEqual(text, "¬it; &unknown; A B ≂̸")
        self.assertEqual(len(text), len(spans))
        self.assertEqual(spans[0], (10, 14))
        self.assertEqual(spans[-1], spans[-2])

    def test_ambiguous_named_references_remain_literal_in_attributes(self):
        anonymizer = TargetedAnonymizer.from_config(
            {"people": [{"values": ["¬it", "¬=", "¬", "©"], "replacement": "[PERSON]"}]}
        )
        source = '<p title="&notit; &not= &copy9">&notit; &not= &copy9</p>'
        self.assertEqual(
            redact_html(source, anonymizer),
            '<p title="&notit; &not= &copy9">[PERSON]; [PERSON] &copy9</p>',
        )
        self.assertEqual(
            redact_html('<p title="&not; &copy!">safe</p>', anonymizer),
            '<p title="[PERSON] [PERSON]!">safe</p>',
        )

    def test_script_and_style_entities_are_literal_not_decoded(self):
        source = (
            '<script>const value = "Max&#32;Mustermann"; /* Max Mustermann */</script>'
            "<style>/* Max&#32;Mustermann; Max Mustermann */</style>"
        )
        self.assertEqual(
            self.redact(source),
            '<script>const value = "Max&#32;Mustermann"; /* [PERSON] */</script>'
            "<style>/* Max&#32;Mustermann; [PERSON] */</style>",
        )

    def test_incomplete_markup_keeps_literal_source(self):
        self.assertEqual(self.redact("<p>Max Mustermann <unfinished"), "<p>[PERSON] <unfinished")

    def test_declarations_and_processing_instructions_are_preserved(self):
        source = "<!DOCTYPE html><?fixture safe?><![CDATA[safe]]><p>safe</p>"
        self.assertEqual(self.redact(source), source)

    def test_malformed_declaration_is_rejected_content_free(self):
        with self.assertRaises(PrivacyValidationError) as caught:
            self.redact("<![Max Mustermann]>")
        self.assertNotIn("Mustermann", str(caught.exception))

    def test_configured_encoded_literal_is_matched_in_raw_source(self):
        anonymizer = TargetedAnonymizer.from_config(
            {"people": [{"values": ["Max&amp;Mustermann"], "replacement": "[PERSON]"}]}
        )
        self.assertEqual(redact_html("<p>Max&amp;Mustermann</p>", anonymizer), "<p>[PERSON]</p>")
