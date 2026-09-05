"""Shared HTTP plumbing for ETL connectors.

Everything here is source-agnostic: session construction, retries, timeouts, TLS,
auth headers, response-envelope unwrapping, batch identifiers and cursor tracking.
A connector should only have to express *how its API paginates* and *how its fields map*.

Three pagination strategies, each independent of *where* the values are sent:

  * `iter_offset_pages()`      - offset/limit
  * `iter_page_number_pages()` - page/pageSize (page NUMBER, not row offset)
  * `iter_next_url_pages()`    - follow an absolute "next" URL until exhausted

`source.request_style` decides whether a paginator's values go in the query string or in
a JSON request body, so a GET `?page=2` API and a POST `{"page": 2}` API share the same
paginator. Where a source publishes a "more pages exist" flag, `response.has_more_key`
uses it in preference to the short-page rule - a full FINAL page is otherwise
indistinguishable from a full middle page.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Generator, Iterable, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 v2 and v1 expose Retry from different paths
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

from api_etl.auth_provider import get_auth_provider
from api_etl.sources.base import DataSource

logger = logging.getLogger(__name__)

RETRY_STATUS = (429, 500, 502, 503, 504)


class BaseHttpSource(DataSource):
    """Base class for HTTP-backed data sources.

    Subclasses implement `pull()`, normally by delegating to one of the iterators here.
    """

    def __init__(self, cfg, auth_provider=None, watermark: Optional[str] = None):
        super().__init__()
        self.cfg = cfg
        self.auth_provider = auth_provider or get_auth_provider(auth_cfg=cfg.auth)
        self.watermark = watermark
        self._observed_cursor: Optional[str] = None
        self._page_no = 0
        self.session = self._build_session()

    # ---------------------------------------------------------------- session

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        source = self.cfg.source
        retry = Retry(
            total=source.retry_total,
            backoff_factor=source.retry_backoff_factor,
            status_forcelist=RETRY_STATUS,
            allowed_methods=None,      # retry POSTs too; ETL pulls are read-only
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    @property
    def verify(self):
        """TLS verification: a CA bundle path if given, else the boolean flag.

        Government APIs frequently present custom or self-signed certificates.
        """
        return self.cfg.source.ca_bundle_path or self.cfg.source.verify_ssl

    def build_headers(self) -> Dict[str, str]:
        return {**(self.cfg.source.headers or {}), **self.auth_provider.get_auth_header()}

    # ---------------------------------------------------------------- cursor

    def observe_cursor(self, value: Any) -> None:
        """Track the high-water mark for incremental sync.

        Treated as an opaque string and compared lexically, which is correct for the
        ISO-8601 timestamps every source seen so far uses. Never parsed here.
        """
        if value in (None, ""):
            return
        value = str(value)
        if self._observed_cursor is None or value > self._observed_cursor:
            self._observed_cursor = value

    @property
    def observed_cursor(self) -> Optional[str]:
        return self._observed_cursor

    def effective_watermark(self) -> Optional[str]:
        """Stored watermark minus the configured overlap.

        Re-pulling an overlap window is free because `external_id` upserts are
        idempotent, and it protects against sources whose submission-time ordering is
        not strictly monotonic against ingest.
        """
        incremental = self.cfg.incremental
        watermark = self.watermark or incremental.initial_cursor
        if not watermark or not incremental.overlap_minutes:
            return watermark
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(watermark[:26], fmt)
            except ValueError:
                continue
            return (parsed - timedelta(minutes=incremental.overlap_minutes)).strftime(fmt)
        logger.debug("api_etl[%s]: watermark %r not a recognised timestamp; using as-is",
                     self.cfg.name, watermark)
        return watermark

    # ---------------------------------------------------------------- request

    def send_style(self, values: Optional[dict] = None) -> dict:
        """Route `values` to the query string or the JSON body per `request_style`.

        A source whose filters are a JSON document and one whose filters are a query
        string differ only in where the same values go, so a paginator builds one dict
        and this decides. `source.body` is merged underneath for the static fields such
        an API wants on every call.
        """
        values = dict(values or {})
        if self.cfg.source.request_style == "json_body":
            return {"json_body": {**(self.cfg.source.body or {}), **values}}
        return {"params": values}

    def request(self, url: Optional[str] = None, params: Optional[dict] = None,
                method: Optional[str] = None, json_body: Optional[dict] = None) -> dict:
        url = url or self.cfg.source.url
        if not url:
            raise self.Error(f"No source URL configured for {self.cfg.name!r}")
        method = method or self.cfg.source.http_method or "GET"

        response = self.session.request(
            method, url,
            headers=self.build_headers(),
            params=params,
            json=json_body,
            timeout=self.cfg.source.timeout_seconds,
            verify=self.verify,
        )
        if not response.ok:
            raise self.Error(f"HTTP request failed: {response.status_code}: {response.reason}")

        try:
            body = response.json()
        except ValueError as exc:
            raise self.Error(f"Response was not JSON: {exc}") from exc

        self._check_envelope(body)
        return body

    def _check_envelope(self, body: Any) -> None:
        """Raise on an API-level error reported inside a 200 response."""
        response_cfg = self.cfg.source.response
        if not response_cfg.status_key or not isinstance(body, dict):
            return
        if not body.get(response_cfg.status_key, True):
            message = body.get(response_cfg.message_key) if response_cfg.message_key else None
            raise self.Error(f"API reported failure: {message or body}")

    def extract_rows(self, body: Any) -> list:
        rows_key = self.cfg.source.response.rows_key
        if rows_key is None:
            if isinstance(body, list):
                return body
            if not body:
                return []
            # Returning the dict here would silently iterate its KEYS downstream and
            # fail much later with "'str' object has no attribute 'get'". Fail loudly
            # at the point the configuration is actually wrong.
            raise self.Error(
                f"{self.cfg.name!r}: response is a {type(body).__name__}, not a list - "
                f"set source.response.rows_key to the field holding the records "
                f"(available keys: {sorted(body)[:10]})"
            )
        rows = (body or {}).get(rows_key)
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise self.Error(
                f"{self.cfg.name!r}: source.response.rows_key={rows_key!r} did not "
                f"resolve to a list (got {type(rows).__name__})"
            )
        return rows

    # ---------------------------------------------------------------- batches

    def next_batch_identifier(self) -> str:
        """Provenance string. Becomes the CSV filename, hence
        IndividualDataSourceUpload.source_name."""
        prefix = self.cfg.provenance.batch_prefix or f"{self.cfg.name}_"
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        return f"{prefix}p{self._page_no:04d}_{stamp}"

    def _emit(self, rows: Iterable[dict]) -> Tuple[list, str]:
        rows = list(rows)
        cursor_field = self.cfg.incremental.cursor_field
        if cursor_field:
            for row in rows:
                self.observe_cursor(row.get(cursor_field))
        return rows, self.next_batch_identifier()

    def _page_limit_reached(self) -> bool:
        max_pages = self.cfg.source.max_pages
        if max_pages and self._page_no >= max_pages:
            logger.warning("api_etl[%s]: stopping at max_pages=%s - the source may have "
                           "more data than was pulled", self.cfg.name, max_pages)
            return True
        return False

    # ---------------------------------------------------------------- paginators

    def iter_offset_pages(self, extra_params: Optional[dict] = None
                          ) -> Generator[Tuple[list, str], None, None]:
        """Offset/limit pagination; stops on a short page."""
        source = self.cfg.source
        offset = 0
        while True:
            if self._page_limit_reached():
                return
            values = {
                **(source.params or {}),
                **(extra_params or {}),
                source.offset_param: offset,
                source.limit_param: source.batch_size,
            }
            body = self.request(**self.send_style(values))
            rows = self.extract_rows(body)
            self._page_no += 1
            if rows:
                yield self._emit(rows)

            has_more = self._has_more(body)
            if has_more is None:
                if len(rows) < source.batch_size:
                    return
            elif not has_more:
                return
            offset += source.batch_size

    def _has_more(self, body: Any) -> Optional[bool]:
        """The envelope's own 'another page exists' flag, if the source reports one.

        Returns None when unconfigured or absent, so a caller falls back to the
        short-page rule. A source that reports it is more reliable than counting rows:
        a full final page is otherwise indistinguishable from a full middle page.
        """
        key = self.cfg.source.response.has_more_key
        if not key or not isinstance(body, dict):
            return None
        current: Any = body
        for part in key.replace(".", "/").split("/"):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
            if current is None:
                return None
        if isinstance(current, bool):
            return current
        if isinstance(current, str):
            return current.strip().lower() in ("true", "1", "yes")
        return bool(current)

    def iter_page_number_pages(self, extra_values: Optional[dict] = None
                               ) -> Generator[Tuple[list, str], None, None]:
        """Page-NUMBER pagination: `page` / `pageSize`, as opposed to offset/limit.

        Values go to the query string or the JSON body per `source.request_style`, so
        this serves both a GET `?page=2` API and a POST `{"page": 2}` one without either
        needing its own paginator.

        Stops on the envelope's `has_more_key` when the source publishes one, else on a
        short page. `first_page` covers 0- and 1-based APIs.
        """
        source = self.cfg.source
        page = source.first_page
        while True:
            if self._page_limit_reached():
                return
            values = {
                **(source.params or {}),
                **(extra_values or {}),
                source.page_param: page,
                source.page_size_param: source.batch_size,
            }
            body = self.request(**self.send_style(values))
            rows = self.extract_rows(body)
            self._page_no += 1
            if rows:
                yield self._emit(rows)

            has_more = self._has_more(body)
            if has_more is None:
                if len(rows) < source.batch_size:
                    return
            elif not has_more:
                return
            page += 1

    def iter_next_url_pages(self, first_params: Optional[dict] = None
                            ) -> Generator[Tuple[list, str], None, None]:
        """Follow an absolute `next` URL until it is null.

        The `next` URL already carries its own paging/query parameters, so params are
        sent on the FIRST request only - re-appending them would corrupt the cursor.
        Headers must still be sent on every request.
        """
        next_key = self.cfg.source.response.next_key
        if not next_key:
            raise self.Error(
                f"{self.cfg.name!r}: iter_next_url_pages requires source.response.next_key"
            )
        url = self.cfg.source.url
        params = {**(self.cfg.source.params or {}), **(first_params or {})}
        while url:
            if self._page_limit_reached():
                return
            body = self.request(url=url, params=params)
            params = None                      # first page only
            rows = self.extract_rows(body)
            self._page_no += 1
            if rows:
                yield self._emit(rows)
            url = (body or {}).get(next_key) if isinstance(body, dict) else None
