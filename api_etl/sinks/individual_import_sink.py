import logging

from api_etl.sinks import DataSink
from api_etl.utils import data_to_file
from core.services import BaseService
from core.models import User
from individual.services import IndividualImportService
from workflow.services import WorkflowService


logger = logging.getLogger(__name__)


WORKFLOW_NAME = "Python Import Individuals"
WORKFLOW_GROUP = "individual"
GROUP_AGGREGATION_COLUMN = None

class IndividualImportSink(DataSink):

    def __init__(self, user: User):
        super().__init__()
        self.service = IndividualImportService(user)

        result = WorkflowService.get_workflows(WORKFLOW_NAME, WORKFLOW_GROUP)
        if not result.get('success'):
            raise self.Error('{}: {}'.format(result.get("message"), result.get("details")))
        workflows = result.get('data', {}).get('workflows')
        if not workflows:
            raise self.Error(f'Workflow not found: group={WORKFLOW_GROUP} name={WORKFLOW_NAME}')
        if len(workflows) > 1:
            raise self.Error(f'Multiple workflows found: group={WORKFLOW_GROUP} name={WORKFLOW_NAME}')
        self.workflow = workflows[0]


    def push(self, data: list[dict], identifier = None):
        import_file = data_to_file(data, identifier)
        result = self.service.import_individuals(
                import_file, self.workflow, GROUP_AGGREGATION_COLUMN)
        logger.debug(f"Completed pushing {len(data)} records with {result}")


