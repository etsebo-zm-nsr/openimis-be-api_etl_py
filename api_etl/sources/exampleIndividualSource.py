import logging

import requests

from api_etl.apps import ApiEtlConfig
from api_etl.auth_provider import get_auth_provider
from api_etl.auth_provider.base import AuthProvider
from api_etl.sources import DataSource
from api_etl.utils import get_timestamped_batch_identifier


logger = logging.getLogger(__name__)


class ExampleIndividualSource(DataSource):
    def __init__(self, auth_provider: AuthProvider = None):
        super().__init__()

        self.auth_provider = auth_provider or get_auth_provider()

    def pull(self):
        """
        Pull the information from the Example Individual API
        This Source yields the list of records in batch
        """
        headers = {
            **ApiEtlConfig.source_headers,
            **self.auth_provider.get_auth_header(),
        }

        method = ApiEtlConfig.source_http_method
        url = ApiEtlConfig.source_url

        logger.info("Pulling individuals from %s %s", method, url)

        in_progress = True
        current_index = 0
        session = requests.Session()
        while in_progress:
            logger.debug("Fetching index: %s, batch size: %s", current_index, ApiEtlConfig.source_batch_size)
            res = session.request(
                method, url, headers=headers,
                params={"current": current_index, "rowCount": ApiEtlConfig.source_batch_size}
            )

            if not res.ok:
                logger.error("HTTP Request failed: %s %s", res.status_code, res.reason)
                raise self.Error(f"Http request failed: {res.status_code}: {res.reason}")

            body = res.json()

            if not body.get("status", True):
                logger.error("HTTP Error response: %s %s", body.get("message"), body.get("result"))
                raise self.Error(f"HTTP Error response: {body.get('message')} {body.get('result')}")

            rows = body.get("rows", [])
            if rows:
                identifier = get_timestamped_batch_identifier(prefix=f"batch_{current_index}_")
                yield rows, identifier

            # Determine if we've reached the last page
            if len(rows) < ApiEtlConfig.source_batch_size:
                in_progress = False
            else:
                current_index += ApiEtlConfig.source_batch_size
