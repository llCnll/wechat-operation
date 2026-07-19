"""Run the dedicated worker for scheduler-created ingestion work."""

from .run_ingestion_worker import Command as _RealtimeIngestionCommand

Command = _RealtimeIngestionCommand
