"""Atomic snapshots of one exclusively reserved public Candidate result."""
from __future__ import annotations

import os
import stat
import tempfile
import time
from pathlib import Path

from release.candidate import canonical_json_bytes


class CandidateResultError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class CandidateResultFile:
    def __init__(self, path: Path):
        self.path = path
        self.cleanup_errors = []
        try:
            with path.open('xb'):
                pass
            self._identity = self._current_identity()
        except FileExistsError as error:
            raise CandidateResultError('CANDIDATE_RESULT_OUTPUT_EXISTS') from error
        except OSError as error:
            raise CandidateResultError('CANDIDATE_RESULT_OUTPUT_UNAVAILABLE') from error

    def _current_identity(self):
        metadata = self.path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or self.path.is_symlink() or self.path.is_junction():
            raise CandidateResultError('CANDIDATE_RESULT_PATH_CHANGED')
        return metadata.st_dev, metadata.st_ino

    def write(self, value):
        temporary = None
        try:
            raw = canonical_json_bytes(value)
            with tempfile.NamedTemporaryFile(mode='wb', dir=self.path.parent,
                    prefix='.' + self.path.name + '.', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(51):
                if self._current_identity() != self._identity:
                    raise CandidateResultError('CANDIDATE_RESULT_PATH_CHANGED')
                try:
                    os.replace(temporary, self.path)
                    temporary = None
                    self._identity = self._current_identity()
                    return
                except OSError as error:
                    if getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 50:
                        raise
                    # Ordinary Windows readers can briefly deny delete-sharing.
                    # Retry only this same reserved output, with a fixed budget.
                    time.sleep(0.1)
        except CandidateResultError:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise CandidateResultError('CANDIDATE_RESULT_WRITE_FAILED') from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    # The prior complete result remains intact. A leftover
                    # public temporary snapshot grants no execution authority.
                    self.cleanup_errors.append('CANDIDATE_RESULT_TEMP_CLEANUP_FAILED')
