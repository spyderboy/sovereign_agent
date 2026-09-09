from abc import ABC, abstractmethod
from typing import List, Tuple, Any

class TaskRunner(ABC):
    """Abstract base class for executing tasks in a specific technology stack."""
    
    @abstractmethod
    def run_task(self, task: str, project_root: str, log_file: str, 
                 max_tier_idx: int, start_tier_idx: int) -> Tuple[bool, str]:
        """Execute a task and return (success, error_summary)."""
        pass

    @abstractmethod
    def validate(self) -> Tuple[bool, str]:
        """Validate the changes made by the worker."""
        pass

    @abstractmethod
    def read_files(self, files: List[str], project_root: str) -> dict:
        """Read specific project files into memory for context."""
        pass

    @abstractmethod
    def write_changes(self, changes: dict, project_root: str, test_only: bool = False) -> Tuple[List[str], str]:
        """Apply changes to project files."""
        pass

    @abstractmethod
    def restore_files(self, files_data: dict, project_root: str) -> None:
        """Rollback changes on failure."""
        pass
