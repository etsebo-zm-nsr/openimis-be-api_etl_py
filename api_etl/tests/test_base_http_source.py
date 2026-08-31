"""Tests for the shared HTTP source: pagination, envelopes, cursors, TLS.

The `next`-URL paginator is the sharpest edge in the core. A `next` URL already carries
its own `start`/`limit`/`query`; re-appending params to it corrupts the cursor and
silently re-reads or skips pages. Auth headers, conversely, MUST be re-sent every time.
Those two opposite rules are one line apart in `iter_next_url_pages`, so they are pinned
here.
"""
from unittest import mock

from django.test import TestCase

from api_etl.config import build_source_config
from api_etl.sources.base_http_source import BaseHttpSource


class _Response:
    def __init__(self, body, ok=True, status_code=200, reason="OK", json_error=False):
        self._body, self.ok, self.status_code, self.reason = body, ok, status_code, reason
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("no json")
        return self._body


class _Source(BaseHttpSource):
    def pull(self):
        yield from self.iter_offset_pages()


def _make(cfg_overrides=None, responses=None, watermark=None, source_cls=_Source):
    cfg = build_source_config("testsrc", cfg_overrides or {})
    src = source_cls(cfg, auth_provider=mock.Mock(get_auth_header=lambda: {"Authorization": "Token t"}),
                     watermark=watermark)
    if responses is not None:
        src.session = mock.Mock()
        src.session.request = mock.Mock(side_effect=list(responses))
    return src


BASE = {"source": {"url": "https://api.example/records", "batch_size": 2,
                   "response": {"rows_key": "rows"}}}


class RequestTestCase(TestCase):

    def test_missing_url_is_a_clear_error(self):
        src = _make({}, responses=[])
        with self.assertRaises(BaseHttpSource.Error) as ctx:
            src.request()
        self.assertIn("No source URL", str(ctx.exception))

    def test_non_2xx_raises(self):
        src = _make(BASE, [_Response(None, ok=False, status_code=503, reason="Unavailable")])
        with self.assertRaises(BaseHttpSource.Error) as ctx:
            src.request()
        self.assertIn("503", str(ctx.exception))

    def test_non_json_body_raises(self):
        src = _make(BASE, [_Response(None, json_error=True)])
        with self.assertRaises(BaseHttpSource.Error) as ctx:
            src.request()
        self.assertIn("not JSON", str(ctx.exception))

    def test_timeout_and_verify_are_passed(self):
        cfg = {"source": dict(BASE["source"], timeout_seconds=42, ca_bundle_path="/ca.pem")}
        src = _make(cfg, [_Response({"rows": []})])
        src.request()
        kwargs = src.session.request.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 42)
        self.assertEqual(kwargs["verify"], "/ca.pem")

    def test_verify_falls_back_to_the_boolean_flag(self):
        src = _make({"source": dict(BASE["source"], verify_ssl=False)}, [_Response({"rows": []})])
        src.request()
        self.assertIs(src.session.request.call_args.kwargs["verify"], False)

    def test_auth_header_is_sent(self):
        src = _make(BASE, [_Response({"rows": []})])
        src.request()
        self.assertEqual(src.session.request.call_args.kwargs["headers"]["Authorization"], "Token t")

    def test_configured_headers_are_merged_with_auth(self):
        src = _make({"source": dict(BASE["source"], headers={"X-Env": "test"})},
                    [_Response({"rows": []})])
        src.request()
        headers = src.session.request.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Env"], "test")
        self.assertIn("Authorization", headers)


class EnvelopeTestCase(TestCase):

    def test_api_level_failure_inside_a_200_raises(self):
        cfg = {"source": dict(BASE["source"],
                              response={"rows_key": "rows", "status_key": "status",
                                        "message_key": "message"})}
        src = _make(cfg, [_Response({"status": False, "message": "bad credentials"})])
        with self.assertRaises(BaseHttpSource.Error) as ctx:
            src.request()
        self.assertIn("bad credentials", str(ctx.exception))

    def test_status_true_passes(self):
        cfg = {"source": dict(BASE["source"], response={"rows_key": "rows", "status_key": "status"})}
        src = _make(cfg, [_Response({"status": True, "rows": [{"id": 1}]})])
        self.assertEqual(src.extract_rows(src.request()), [{"id": 1}])


class ExtractRowsTestCase(TestCase):

    def test_rows_key(self):
        src = _make(BASE, [])
        self.assertEqual(src.extract_rows({"rows": [{"id": 1}]}), [{"id": 1}])

    def test_missing_rows_key_yields_empty(self):
        src = _make(BASE, [])
        self.assertEqual(src.extract_rows({"other": []}), [])

    def test_bare_list_when_no_rows_key(self):
        src = _make({"source": {"url": "u"}}, [])
        self.assertEqual(src.extract_rows([{"id": 1}]), [{"id": 1}])

    def test_dict_without_rows_key_raises_instead_of_iterating_keys(self):
        """The silent-corruption case: returning the dict iterates its KEYS downstream
        and fails much later with an opaque 'str' object has no attribute 'get'."""
        src = _make({"source": {"url": "u"}}, [])
        with self.assertRaises(BaseHttpSource.Error) as ctx:
            src.extract_rows({"results": [{"id": 1}], "count": 1})
        message = str(ctx.exception)
        self.assertIn("rows_key", message)
        self.assertIn("results", message, "the error should name the available keys")

    def test_rows_key_resolving_to_a_non_list_raises(self):
        src = _make(BASE, [])
        with self.assertRaises(BaseHttpSource.Error):
            src.extract_rows({"rows": {"not": "a list"}})

    def test_empty_body_is_empty(self):
        src = _make({"source": {"url": "u"}}, [])
        self.assertEqual(src.extract_rows(None), [])


class CursorTestCase(TestCase):

    def test_observe_cursor_keeps_the_maximum(self):
        src = _make(BASE, [])
        for value in ("2026-01-02", "2026-01-05", "2026-01-03"):
            src.observe_cursor(value)
        self.assertEqual(src.observed_cursor, "2026-01-05")

    def test_blank_values_are_ignored(self):
        src = _make(BASE, [])
        src.observe_cursor(None)
        src.observe_cursor("")
        self.assertIsNone(src.observed_cursor)

    def test_effective_watermark_subtracts_the_overlap(self):
        src = _make({"incremental": {"enabled": True, "overlap_minutes": 15}},
                    [], watermark="2026-01-02T12:00:00")
        self.assertEqual(src.effective_watermark(), "2026-01-02T11:45:00")

    def test_zero_overlap_returns_the_watermark_unchanged(self):
        src = _make({"incremental": {"enabled": True, "overlap_minutes": 0}},
                    [], watermark="2026-01-02T12:00:00")
        self.assertEqual(src.effective_watermark(), "2026-01-02T12:00:00")

    def test_unparseable_watermark_is_used_as_is(self):
        """The cursor is an opaque string; a non-timestamp source must still work."""
        src = _make({"incremental": {"enabled": True, "overlap_minutes": 15}},
                    [], watermark="opaque-token-42")
        self.assertEqual(src.effective_watermark(), "opaque-token-42")

    def test_initial_cursor_is_used_when_no_watermark_stored(self):
        src = _make({"incremental": {"enabled": True, "overlap_minutes": 0,
                                     "initial_cursor": "2026-01-01T00:00:00"}}, [])
        self.assertEqual(src.effective_watermark(), "2026-01-01T00:00:00")

    def test_cursor_is_observed_from_emitted_rows(self):
        cfg = {"source": dict(BASE["source"], batch_size=10),
               "incremental": {"enabled": True, "cursor_field": "updated"}}
        src = _make(cfg, [_Response({"rows": [{"id": 1, "updated": "2026-01-01T00:00:00"},
                                              {"id": 2, "updated": "2026-01-09T00:00:00"}]})])
        list(src.pull())
        self.assertEqual(src.observed_cursor, "2026-01-09T00:00:00")


class OffsetPaginationTestCase(TestCase):

    def test_stops_on_a_short_page(self):
        src = _make(BASE, [
            _Response({"rows": [{"id": 1}, {"id": 2}]}),
            _Response({"rows": [{"id": 3}]}),
        ])
        pages = list(src.pull())
        self.assertEqual([len(rows) for rows, _ in pages], [2, 1])
        self.assertEqual(src.session.request.call_count, 2)

    def test_offset_advances_by_batch_size(self):
        src = _make(BASE, [
            _Response({"rows": [{"id": 1}, {"id": 2}]}),
            _Response({"rows": []}),
        ])
        list(src.pull())
        offsets = [c.kwargs["params"]["current"] for c in src.session.request.call_args_list]
        self.assertEqual(offsets, [0, 2])

    def test_empty_first_page_yields_nothing(self):
        src = _make(BASE, [_Response({"rows": []})])
        self.assertEqual(list(src.pull()), [])

    def test_configured_params_are_included(self):
        cfg = {"source": dict(BASE["source"], params={"region": "ZM-01"})}
        src = _make(cfg, [_Response({"rows": []})])
        list(src.pull())
        self.assertEqual(src.session.request.call_args.kwargs["params"]["region"], "ZM-01")

    def test_max_pages_circuit_breaker_trips(self):
        cfg = {"source": dict(BASE["source"], max_pages=2)}
        src = _make(cfg, [_Response({"rows": [{"id": i}, {"id": i + 1}]}) for i in range(10)])
        pages = list(src.pull())
        self.assertEqual(len(pages), 2)
        self.assertEqual(src.session.request.call_count, 2)

    def test_batch_identifier_is_unique_per_page_and_prefixed(self):
        cfg = {"source": dict(BASE["source"]), "provenance": {"batch_prefix": "zispis_"}}
        src = _make(cfg, [
            _Response({"rows": [{"id": 1}, {"id": 2}]}),
            _Response({"rows": [{"id": 3}]}),
        ])
        identifiers = [ident for _, ident in src.pull()]
        self.assertEqual(len(set(identifiers)), 2)
        for ident in identifiers:
            self.assertTrue(ident.startswith("zispis_"), ident)


class _NextUrlSource(BaseHttpSource):
    def pull(self):
        yield from self.iter_next_url_pages(first_params={"format": "json", "limit": 2})


NEXT_BASE = {"source": {"url": "https://kf.example/api/v2/data",
                        "batch_size": 2,
                        "response": {"rows_key": "results", "next_key": "next"}}}


class NextUrlPaginationTestCase(TestCase):

    def test_follows_next_until_null(self):
        src = _make(NEXT_BASE, [
            _Response({"results": [{"id": 1}], "next": "https://kf.example/p2"}),
            _Response({"results": [{"id": 2}], "next": None}),
        ], source_cls=_NextUrlSource)
        pages = list(src.pull())
        self.assertEqual(len(pages), 2)
        urls = [c.args[1] for c in src.session.request.call_args_list]
        self.assertEqual(urls, ["https://kf.example/api/v2/data", "https://kf.example/p2"])

    def test_params_are_sent_on_the_first_request_only(self):
        """The `next` URL already embeds start/limit/query - re-appending corrupts it."""
        src = _make(NEXT_BASE, [
            _Response({"results": [{"id": 1}], "next": "https://kf.example/p2?start=2"}),
            _Response({"results": [{"id": 2}], "next": None}),
        ], source_cls=_NextUrlSource)
        list(src.pull())
        calls = src.session.request.call_args_list
        self.assertIsNotNone(calls[0].kwargs["params"])
        self.assertIsNone(calls[1].kwargs["params"],
                          "params re-sent on a next URL would corrupt the cursor")

    def test_headers_are_re_sent_on_every_request(self):
        """Opposite rule to params, one line away in the same loop."""
        src = _make(NEXT_BASE, [
            _Response({"results": [{"id": 1}], "next": "https://kf.example/p2"}),
            _Response({"results": [{"id": 2}], "next": None}),
        ], source_cls=_NextUrlSource)
        list(src.pull())
        for call in src.session.request.call_args_list:
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Token t")

    def test_missing_next_key_config_is_a_clear_error(self):
        cfg = {"source": {"url": "u", "response": {"rows_key": "results"}}}
        src = _make(cfg, [], source_cls=_NextUrlSource)
        with self.assertRaises(BaseHttpSource.Error) as ctx:
            list(src.pull())
        self.assertIn("next_key", str(ctx.exception))

    def test_max_pages_applies_to_next_url_paging_too(self):
        cfg = {"source": dict(NEXT_BASE["source"], max_pages=2)}
        src = _make(cfg, [_Response({"results": [{"id": i}], "next": "https://kf.example/n"})
                          for i in range(10)], source_cls=_NextUrlSource)
        self.assertEqual(len(list(src.pull())), 2)
