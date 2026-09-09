import os
import shutil
from typing import List, Tuple
from lib.runner.base import TaskRunner

class FlutterTaskRunner(TaskRunner):
    """Adapter for Flutter/Flame projects."""
    
    def run_task(self, task: str, project_root: str, log_file: str, 
                 max_tier_idx: int, start_tier_idx: int) -> Tuple[bool, str]:
        # Move existing work.py logic here eventually
        from work import run_task as original_run_task
        return original_run_task(task, project_root, log_file, max_tier_idx, start_tier_idx)

    def validate(self) -> Tuple[bool, str]:
        from work import validate as original_validate
        return original_validate()

    def read_files(self, files: List[str], project_root: str) -> dict:
        from work import read_files as original_read_files
        return original_read_files(files, project_root)

    def write_changes(self, changes: dict, project_root: str, test_only: bool = False) -> Tuple[List[str], str]:
        from work import write_changes as original_write_changes
        return original_write_changes(changes, project_root, test_only)

    def restore_files(self, files_data: dict, project_root: str) -> None:
        from work import restore_files as original_restore_files
        original_restore_files(files_data, project_root)
