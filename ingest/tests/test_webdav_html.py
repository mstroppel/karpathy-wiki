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

    def test_cross_tag_full_name_wins_over_different_short_placeholder(self):
        anonymizer = TargetedAnonymizer.from_config(
            {
                "people": [
                    {"values": ["Max"], "replacement": "[FIRST]"},
                    {"values": ["Max Mustermann"], "replacement": "[FULL]"},
                ]
            }
        )
        self.assertEqual(
            redact_html("<p>Max <b>Mustermann</b></p>", anonymizer), "<p>[FULL]<b></b></p>"
        )

    def test_raw_longer_rule_wins_over_decoded_short_placeholder(self):
        anonymizer = TargetedAnonymizer.from_config(
            {
                "people": [
                    {"values": ["Max"], "replacement": "[FIRST]"},
                    {"values": ["Max&amp;Mustermann"], "replacement": "[FULL]"},
                ]
            }
        )
        self.assertEqual(
            redact_html("<p>Max&amp;Mustermann; Max</p>", anonymizer),
            "<p>[FULL]; [FIRST]</p>",
        )

    def test_located_matches_expose_literal_then_phone_rule_precedence(self):
        anonymizer = TargetedAnonymizer.from_config(
            {
                "people": [
                    {"values": ["Max"], "replacement": "[FIRST]"},
                    {"values": ["Max Mustermann"], "replacement": "[FULL]"},
                ],
                "phones": [{"values": ["+49 123 456789"], "replacement": "[PHONE]"}],
            }
        )
        matches = anonymizer.locate("Max Mustermann; Max; +49 123 456789")
        self.assertEqual(
            [(match.replacement, match.priority) for match in matches],
            [("[FULL]", 0), ("[FIRST]", 1), ("[PHONE]", 2)],
        )

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

    def test_decl_data_match_is_rejected_content_free(self):
        # Values inside declaration / processing-instruction data are not
        # matchable: blanket protection quarantines the generation fail-closed
        # (documented in docs/configuration.md).
        with self.assertRaises(PrivacyValidationError) as caught:
            self.redact('<!DOCTYPE d SYSTEM "Max Mustermann.dtd">')
        self.assertNotIn("Mustermann", str(caught.exception))

    def test_decl_terminator_inside_quotes_is_protected(self):
        # `>` inside quoted declaration data does not terminate the construct;
        # a value claimed by the data must not delete the real terminators.
        anonymizer = TargetedAnonymizer.from_config(
            {"people": [{"values": ['y"> ]>Max Mustermann'], "replacement": "[PERSON]"}]}
        )
        with self.assertRaises(PrivacyValidationError):
            redact_html('<!DOCTYPE d [<!ENTITY a "x>y"> ]>Max Mustermann', anonymizer)

    def test_end_tag_terminator_inside_quoted_attributes_is_protected(self):
        anonymizer = TargetedAnonymizer.from_config(
            {"people": [{"values": [">Max Mustermann"], "replacement": "[PERSON]"}]}
        )
        with self.assertRaises(PrivacyValidationError):
            redact_html('text before </p attr="a>b">Max Mustermann', anonymizer)

    def test_bogus_comment_closing_bracket_is_protected(self):
        # Bogus comments (`<!...>`) arrive through handle_comment with a
        # 2-character opener; their closing `>` must survive any redaction.
        anonymizer = TargetedAnonymizer.from_config(
            {"people": [{"values": ["safe >Max Mustermann"], "replacement": "[PERSON]"}]}
        )
        with self.assertRaises(PrivacyValidationError):
            redact_html("<! safe >Max Mustermann", anonymizer)

    def test_bogus_comment_body_is_matched_and_protects_syntax(self):
        self.assertEqual(self.redact("<! Max Mustermann <! tail>tail"), "<! [PERSON] <! tail>tail")

    def test_unterminated_comment_value_is_rejected(self):
        with self.assertRaises(PrivacyValidationError):
            self.redact("<!-- Max Mustermann")
        self.assertEqual(
            self.redact("<!-- only text without matches"), "<!-- only text without matches"
        )

    def test_configured_encoded_literal_is_matched_in_raw_source(self):
        anonymizer = TargetedAnonymizer.from_config(
            {"people": [{"values": ["Max&amp;Mustermann"], "replacement": "[PERSON]"}]}
        )
        self.assertEqual(redact_html("<p>Max&amp;Mustermann</p>", anonymizer), "<p>[PERSON]</p>")
