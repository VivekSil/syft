import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from syftbox.client.exceptions import SyftServerError
from syftbox.client.plugins.sync.datasite_state import DatasiteState
from syftbox.client.plugins.sync.exceptions import (
    FatalSyncError,
    SyftPermissionError,
    SyncEnvironmentError,
    SyncValidationError,
)
from syftbox.client.plugins.sync.local_state import LocalState
from syftbox.client.plugins.sync.queue import SyncQueue, SyncQueueItem
from syftbox.client.plugins.sync.sync_action import SyncAction, determine_sync_action
from syftbox.client.plugins.sync.sync_client import SyncClient
from syftbox.client.plugins.sync.types import SyncActionType
from syftbox.lib.hash import hash_file
from syftbox.lib.ignore import filter_ignored_paths
from syftbox.server.models.sync_models import FileMetadata


def create_local_batch(sync_client: SyncClient, paths_to_download: list[Path]) -> list[str]:
    try:
        content_bytes = sync_client.download_bulk(paths_to_download)
    except SyftServerError as e:
        logger.error(e)
        return []
    zip_file = zipfile.ZipFile(BytesIO(content_bytes))
    zip_file.extractall(sync_client.workspace.datasites)
    return zip_file.namelist()


class SyncConsumer:
    def __init__(self, client: SyncClient, queue: SyncQueue, local_state: LocalState):
        self.client = client
        self.queue = queue
        self.local_state = local_state

    def validate_sync_environment(self):
        if not Path(self.client.workspace.datasites).is_dir():
            raise SyncEnvironmentError("Your sync folder has been deleted by a different process.")
        if not self.local_state.path.is_file():
            raise SyncEnvironmentError("Your previous sync state has been deleted by a different process.")

    def consume_all(self):
        while not self.queue.empty():
            self.validate_sync_environment()
            item = self.queue.get(timeout=0.1)
            try:
                self.process_filechange(item)
            except FatalSyncError as e:
                # Fatal error, syncing should be interrupted
                raise e
            except Exception as e:
                logger.error(f"Failed to sync file {item.data.path}, it will be retried in the next sync. Reason: {e}")

    def download_all_missing(self, datasite_states: list[DatasiteState]):
        try:
            missing_files: list[Path] = []
            for datasite_state in datasite_states:
                for file in datasite_state.remote_state:
                    path = file.path
                    if not self.local_state.states.get(path):
                        missing_files.append(path)
            missing_files = filter_ignored_paths(self.client.workspace.datasites, missing_files)

            logger.info(f"Downloading {len(missing_files)} files in batch")
            received_files = create_local_batch(self.client, missing_files)
            for path in received_files:
                path = Path(path)
                state = self.get_current_local_metadata(path)
                self.local_state.insert_synced_file(
                    path=path,
                    state=state,
                    action=SyncActionType.CREATE_LOCAL,
                )
        except FatalSyncError as e:
            raise e
        except Exception as e:
            logger.error(
                f"Failed to download missing files, files will be downloaded individually instead. Reason: {e}"
            )

    def determine_action(self, item: SyncQueueItem) -> SyncAction:
        path = item.data.path
        current_local_metadata = self.get_current_local_metadata(path)
        previous_local_metadata = self.get_previous_local_metadata(path)
        current_remote_metadata = self.get_current_remote_metadata(path)

        return determine_sync_action(
            current_local_metadata=current_local_metadata,
            previous_local_metadata=previous_local_metadata,
            current_remote_metadata=current_remote_metadata,
        )

    def process_action(self, action: SyncAction) -> SyncAction:
        """
        Execute an action and handle any exceptions that may occur. Actions are either:
        - Executed successfully (status = SYNCED)
        - Rejected by the server (status = REJECTED). Rejection behaviour is defined by the action.
            For example, a rejected local deletion will be reverted by creating the file again.
        - Error occurred during execution (status = ERROR), the action will be retried in the next sync.
            Errors could be either validation errors (file is too large, etc.) or server errors (connection issues, etc.)
        """
        try:
            logger.info(action.info_message)
            action.validate(self.client)
            action.execute(self.client)
        except SyftPermissionError as e:
            action.process_rejection(self.client, reason=str(e))
        except SyncValidationError as e:
            # TODO Should we reject validation errors as well?
            action.error(e)
            logger.warning(f"Validation error: {e}")
        except (SyftServerError, httpx.RequestError) as e:
            action.error(e)
            logger.error(f"Failed to sync file {action.path}, it will be retried in the next sync. Reason: {e}")

        return action

    def process_filechange(self, item: SyncQueueItem) -> None:
        action = self.determine_action(item)
        if action.is_noop():
            return

        action = self.process_action(action)
        self.local_state.insert_completed_action(action)

    def get_current_local_metadata(self, path: Path) -> Optional[FileMetadata]:
        abs_path = self.client.workspace.datasites / path
        if not abs_path.is_file():
            return None
        return hash_file(abs_path, root_dir=self.client.workspace.datasites)

    def get_previous_local_metadata(self, path: Path) -> Optional[FileMetadata]:
        return self.local_state.states.get(path, None)

    def get_current_remote_metadata(self, path: Path) -> Optional[FileMetadata]:
        try:
            return self.client.get_metadata(path)
        except SyftServerError:
            return None
